# -*- coding: utf-8 -*-
"""群感知插件增强逻辑的本地测试（不依赖线上 MaiBot）。

思路：stub 掉 maibot_sdk（本地没有），用 importlib 按包加载真实的
group-awareness-plugin/plugin.py，实例化后对改动的三个方法做真实驱动：
- _count_probation_message：普通群聊消息按发送者累计考察名单发言数
- _judge_normality：LLM 判号输出 → 正常/异常（异常才移出，保守保留）
- _check_probation：进群登记 → 计数 → 到期判号 → 转正/移出全链路决策

用法：python test_enhance.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
import time
import types
from types import SimpleNamespace
from typing import Any

PLUGIN_DIR = __file__.rstrip("test_enhance.py").rstrip("/\\").rstrip("\\/")
# PLUGIN_DIR = r"D:\Hanako-workspace\group-awareness-plugin"


# ===== 1. stub maibot_sdk（本地没有，最小可用替身） =====

def _stub_module(name: str, **attrs: Any) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class MaiBotPlugin:
    """基类替身：真实插件的 __init__ 只自设非依赖 ctx 的属性，这里空即可。"""
    def __init__(self) -> None:
        pass


def _hook(*args: Any, **kwargs: Any):
    """@HookHandler(...) 装饰器替身：接受任意参数（位置 hook 名 + 关键字配置），
    原样返回被装饰方法。"""
    def deco(fn):
        return fn
    return deco


class _Tool:
    """@Tool(name, ...) 装饰器替身：把函数收进 list，测试用。"""
    def __init__(self, name: str, **kwargs: Any):
        self.name = name
        self.kwargs = kwargs
    def __call__(self, fn):
        fn._tool_name = self.name
        fn._tool_kwargs = self.kwargs
        return fn


class _ToolParameterInfo:
    def __init__(self, **kwargs: Any):
        self.__dict__.update(kwargs)


# 顶层符号
_stub_module("maibot_sdk",
    MaiBotPlugin=MaiBotPlugin,
    HookHandler=_hook,
)
_stub_module("maibot_sdk.components",
    Tool=_Tool,
    ToolParameterInfo=_ToolParameterInfo,
)
_stub_module("maibot_sdk.types",
    ErrorPolicy=SimpleNamespace(SKIP="skip"),
    HookMode=SimpleNamespace(BLOCKING="blocking"),
    HookOrder=SimpleNamespace(EARLY="early"),
    ToolParamType=SimpleNamespace(STRING="string"),
)

# 注入空包名，让 plugin.py 的相对导入 `.config` 解析到我们构造的假 config 模块
_pkg = types.ModuleType("ga_plugin")
sys.modules["ga_plugin"] = _pkg

_fake_config = types.ModuleType("ga_plugin.config")
_fake_config.GroupAwarenessConfig = type("GroupAwarenessConfig", (), {})
_fake_config.ProbationConfig = type("ProbationConfig", (), {})
sys.modules["ga_plugin.config"] = _fake_config


def _load_plugin_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("ga_plugin.plugin", __import__("os").path.join(PLUGIN_DIR, "plugin.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ga_plugin.plugin"] = mod
    spec.loader.exec_module(mod)
    return mod


# ===== 2. 驱动环境：mock ctx / config =====

class _Logger:
    def __init__(self):
        self.lines = []
    def info(self, *a, **k):
        self.lines.append(("info", a))
    def warning(self, *a, **k):
        self.lines.append(("warn", a))
    def debug(self, *a, **k):
        self.lines.append(("dbg", a))


def _make_cfg(**overrides):
    base = dict(
        enabled=True, probation_hours=48, check_interval_minutes=30,
        min_messages=0, llm_judge=False, llm_judge_prompt="",
        reject_add_request=False, whitelist=[],
    )
    base.update(overrides)
    greet = SimpleNamespace(mode="template", fallback_to_template=True, template="欢迎新人～")
    kick = SimpleNamespace(mode="template", fallback_to_template=True,
                           template="成员 {member_name}（QQ {user_id}）加入超过 {probation_hours} 小时未发言，已移出。")
    llm = SimpleNamespace(model="planner", temperature=0.8, max_tokens=128)
    cfg = SimpleNamespace(
        enabled=base["enabled"], probation_hours=base["probation_hours"],
        check_interval_minutes=base["check_interval_minutes"],
        min_messages=base["min_messages"], llm_judge=base["llm_judge"],
        llm_judge_prompt=base["llm_judge_prompt"],
        reject_add_request=base["reject_add_request"], whitelist=base["whitelist"],
        greet=greet, kick_message=kick, llm=llm,
    )
    return cfg


def _build_plugin(mod, cfg, member_info=None, llm_answer=None):
    """实例化并注入 mock ctx/config/api。"""
    plugin = mod.GroupAwarenessPlugin()
    plugin.config = SimpleNamespace(
        plugin=SimpleNamespace(enabled=True, record_changes=False),
        probation=cfg,
    )
    logger = _Logger()
    api = _FakeApi(member_info=member_info)
    llm_provider = _FakeLlm(answer=llm_answer)
    plugin.ctx = SimpleNamespace(
        logger=logger,
        api=api,
        llm=llm_provider,
        config=SimpleNamespace(get=lambda key, default="": default),
    )
    # probation.json 落到临时目录
    plugin._probation_file = None  # 关闭持久化写盘（内存验证即可）
    return plugin


class _FakeApi:
    """mock ctx.api.call；考察期只用到 get_group_member_info / set_group_kick /
    send_group_msg。member_info 为每 user 的 fixed dict。"""
    def __init__(self, member_info=None):
        self.member_info = member_info or {}
        self.kicked = []  # (group, user)
        self.sent = []
    async def call(self, name, **kwargs):
        if name == "adapter.napcat.group.get_group_member_info":
            uid = str(kwargs.get("user_id"))
            return self.member_info.get(uid, {})
        if name == "adapter.napcat.group.set_group_kick":
            self.kicked.append((kwargs.get("group_id"), kwargs.get("user_id")))
            return {}
        if name == "adapter.napcat.group.send_group_msg":
            self.sent.append(kwargs)
            return {}
        return {}


class _FakeLlm:
    """mock ctx.llm.generate；按 answer 给出判号输出。

    与真实 _extract_llm_text 取 result["response"] 的契约对齐。
    """
    def __init__(self, answer="正常"):
        self.answer = answer
    async def generate(self, **kwargs):
        return {"response": self.answer}


# ===== 3. 测试用例 =====

def _mk_message(group_id, user_id, **extra):
    return {
        "session_id": "s1",
        "message_info": {
            "group_id": group_id,
            "user_id": user_id,
            **extra,
        },
    }


TESTS = []

def test(name):
    def deco(fn):
        TESTS.append((name, fn))
        return fn
    return deco


@test("A. 普通消息计数：进群后名单成员发言累计")
async def t_count():
    mod = _load_plugin_module()
    cfg = _make_cfg(enabled=True, min_messages=3)
    p = _build_plugin(mod, cfg)

    # 进群登记（真实 _handle_probation_join）
    join = _mk_message("1001", "u1")
    join["is_notify"] = True
    join["message_info"]["additional_config"] = {
        "napcat_notice_type": "group_increase",
        "napcat_notice_sub_type": "increase",
        "napcat_notice_payload": {"group_id": "1001", "user_id": "u1"},
    }
    await p._handle_probation_join({"event": "group_increase", "group_id": "1001",
                                    "user_id": "u1", "member_name": "新人甲"})
    assert p._probation["1001"]["u1"] is not None

    # 名单成员 + 非名单成员各发 2 条
    for _ in range(2):
        p._count_probation_message(_mk_message("1001", "u1"))
        p._count_probation_message(_mk_message("1001", "路人"))
    assert p._probation["1001"]["u1"]["message_count"] == 2, p._probation["1001"]["u1"]
    print("  A ok: 名单成员计数 2，非名单成员 路人 未进入考察名单（未累计）")


@test("B. user_id 提取兼容：user_info 主路径 + 兜底形态")
async def t_extract():
    mod = _load_plugin_module()
    cfg = _make_cfg(min_messages=1)
    p = _build_plugin(mod, cfg)
    p._probation = {"1001": {"u9": {"join_ts": time.time(), "member_name": "n", "message_count": 0}}}
    # 主路径：message_info.user_info.user_id（宿主真实结构）
    m1 = {"session_id": "s", "message_info": {"group_id": "1001",
                                               "user_info": {"user_id": "u9"}}}
    p._count_probation_message(m1)
    # 顶层 user_id 形态
    p._count_probation_message(_mk_message("1001", "u9"))
    # 嵌套 sender.user_id 形态
    m3 = {"session_id": "s", "message_info": {"group_id": "1001",
                                               "sender": {"user_id": "u9"}}}
    p._count_probation_message(m3)
    assert p._probation["1001"]["u9"]["message_count"] == 3, p._probation["1001"]["u9"]
    print("  B ok: user_info 主路径 + 顶层 + sender 三种形态均识别（count=3）")


@test("C. LLM 判号输出映射：异常才移出，其余保守保留")
async def t_judge():
    mod = _load_plugin_module()
    for answer, expect_abnormal in [("异常", True), ("机器人号", True), ("广告", True),
                                    ("正常", False), ("正常用户", False), ("不确定", False), ("", False)]:
        cfg = _make_cfg(llm_judge=True)
        p = _build_plugin(mod, cfg, llm_answer=answer)
        p._probation = {"1001": {"u5": {"join_ts": time.time()-3600, "member_name": "z", "message_count": 1}}}
        normal = await p._judge_normality("u5", p._probation["1001"]["u5"])
        assert (not normal) == expect_abnormal, (answer, normal)
        print(f"  C ok: LLM说「{answer!r}」 -> {'异常(应移出)' if not normal else '保留'}（预期 {'移出' if expect_abnormal else '保留'}）")


def _join_entry(join_ts, count):
    return {"join_ts": join_ts, "member_name": "新人", "message_count": count}


@test("D. 到期判号全链路：判异常移出 / 判正常转正保留")
async def t_check_judge():
    mod = _load_plugin_module()
    cfg = _make_cfg(min_messages=3, llm_judge=True, probation_hours=0.01)
    # 判异常
    p = _build_plugin(mod, cfg, llm_answer="异常")
    p._probation = {"1001": {"400002": _join_entry(time.time()-60, 1)}}
    await p._check_probation()
    assert (1001, 400002) in p.ctx.api.kicked, p.ctx.api.kicked
    # 判正常 → 转正保留，不移出
    p2 = _build_plugin(mod, _make_cfg(min_messages=3, llm_judge=True, probation_hours=0.01), llm_answer="正常")
    p2._probation = {"1001": {"400003": _join_entry(time.time()-60, 1)}}
    await p2._check_probation()
    assert not p2.ctx.api.kicked and "400003" not in p2._probation.get("1001", {}), (p2.ctx.api.kicked, p2._probation)
    print("  D ok: 判异常→移出；判正常→转正保留不移出")


@test("E. 消息数阈值转正：达阈值即转正；未达未到期保留")
async def t_threshold_convert():
    mod = _load_plugin_module()
    cfg = _make_cfg(min_messages=2, llm_judge=False)
    now = time.time()
    # 已达阈值 → 转正
    p = _build_plugin(mod, cfg)
    p._probation = {"1001": {"600006": _join_entry(now-3600, 2)}}
    await p._check_probation()
    assert "600006" not in p._probation.get("1001", {}), p._probation
    # 未达阈值且未到期 → 保留等待
    p2 = _build_plugin(mod, _make_cfg(min_messages=5, llm_judge=False))
    p2._probation = {"1001": {"700007": _join_entry(now-10, 3)}}
    await p2._check_probation()
    assert "700007" in p2._probation["1001"], p2._probation
    print("  E ok: 达阈值转正；未达未到期保留")


@test("G. 判号提示词可配置：自定义模板占位符替换")
async def t_custom_prompt():
    mod = _load_plugin_module()
    captured = {}

    async def _run(prompt_tpl, min_msg, want_progress):
        cfg = _make_cfg(llm_judge=True, min_messages=min_msg, llm_judge_prompt=prompt_tpl)
        p = _build_plugin(mod, cfg, llm_answer="异常")
        p._probation = {"1001": {"800001": _join_entry(time.time()-3600, 3)}}
        orig = p.ctx.llm.generate
        async def gen(**kw):
            captured["prompt"] = kw.get("prompt")
            return await orig(**kw)
        p.ctx.llm.generate = gen
        return await p._judge_normality("800001", p._probation["1001"]["800001"]), captured["prompt"][-1]["content"]

    # 自定义模板 + min_messages>0
    _, up = await _run("判号：{member_name}({user_id})发言{message_count}条，{progress}，是否正常？", 5, "")
    assert "判号：新人(800001)发言3条" in up, up
    assert "考察标准是达到 5 条发言，TA 没达" in up, up
    assert "是否正常？" in up, up
    # min_messages=0 时 progress 换成未转正原由
    _, up0 = await _run("判号：{member_name} {progress}", 0, "")
    assert "考察期内一直没怎么发言" in up0, up0
    print("  G ok: 自定义判号提示词占位符正确替换（昵称/QQ/发言数/progress）；min_messages>0 与 =0 的 progress 分支均生效")


@test("F. 未启用 min_messages 时保持原行为")
async def t_legacy():
    mod = _load_plugin_module()
    now = time.time()
    # 发过言（last_sent>join）→ 转正
    cfg = _make_cfg(min_messages=0, llm_judge=False)
    p = _build_plugin(mod, cfg, member_info={"u8": {"role": "member", "last_sent_time": now}})
    p._probation = {"1001": {"u8": _join_entry(now-3600, 0)}}
    await p._check_probation()
    assert "u8" not in p._probation.get("1001", {}), p._probation
    # 未发言且到期 → 移出（原行为）
    p2 = _build_plugin(mod, _make_cfg(min_messages=0, llm_judge=False, probation_hours=0.01))
    p2._probation = {"1001": {"900009": _join_entry(now-60, 0)}}
    p2.ctx.api.member_info = {}
    await p2._check_probation()
    assert (1001, 900009) in p2.ctx.api.kicked, p2.ctx.api.kicked
    print("  F ok: 不启用阈值时——发过言即转正；到期未发言即移出（原行为保留）")


async def _main():
    for name, fn in TESTS:
        print(f"== {name} ==")
        await fn()
    print("\n全部通过 ✓")


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except Exception as e:
        import traceback
        traceback.print_exc()
        sys.exit(1)