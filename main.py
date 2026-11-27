"""在 QQ 里一键切换 AstrBot 全局主模型，并同步写死模型的后台插件。

只改配置与运行时 provider，不重启实例；失败按备份全量回滚。
"""

import asyncio
import copy
import json
import os
import re
import shutil
import tempfile
import time
from datetime import datetime
from typing import Any, AsyncGenerator

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.provider.entities import ProviderType

try:
    from astrbot.core.star.star import star_map
except Exception:  # pragma: no cover
    star_map = {}

PLUGIN_ID = "君莫笑/astrbot_plugin_model_switch"
PRIMARY = ("agent_runner", "config", "model", "provider_id")
FALLBACK = ("agent_runner", "config", "model", "fallback_provider_ids")

# (文件, 路径, 显示名, 类型)  id=provider id, list=id 数组, model=模型名
TEXT_FIELDS = (
    ("astrbot_plugin_humanoid_core_config.json", ("mood_provider_name",), "humanoid_core 情绪分析", "id"),
    ("astrbot_plugin_livingmemory_config.json", ("provider_settings", "llm_provider_id"), "livingmemory 记忆", "id"),
    ("astrbot_plugin_autonomous_social_config.json", ("proactive_provider_ids",), "autonomous_social 主动消息", "list"),
    ("astrbot_plugin_clonetts_config.json", ("llm_recognition",), "clonetts 语音识别", "id"),
)
MULTI_FIELDS = (
    ("cmd_config.json", ("provider_settings", "default_image_caption_provider_id"), "图片描述", "id"),
    ("cmd_config.json", ("provider_ltm_settings", "image_caption_provider_id"), "长期记忆图片描述", "id"),
    ("meme_manager_config.json", ("semantic", "vision_provider_id"), "meme_manager 语义", "id"),
    ("meme_manager_config.json", ("auto_collect", "vision_provider_id"), "meme_manager 自动收集", "id"),
    ("astrbot_plugin_stealer_config.json", ("vision_provider_id",), "stealer 视觉", "id"),
    ("astrbot_plugin_group_chat_plus_config.json", ("image_to_text_provider_id",), "群聊图片理解", "id"),
    ("astrbot_plugin_group_chat_plus_config.json", ("private_image_to_text_provider_id",), "私聊图片理解", "id"),
    ("astrbot_plugin_community_radar_config.json", ("video", "provider_id"), "社区雷达视频", "id"),
    ("astrbot_plugin_community_radar_config.json", ("video", "audio_provider_id"), "社区雷达音频", "id"),
    ("astrbot_plugin_memory_diary_config.json", ("image_model",), "记忆日记生图", "model"),
    ("astrbot_plugin_memory_diary_config.json", ("vision_api_model",), "记忆日记视觉", "model"),
)
TEXT_FILES = {x[0] for x in TEXT_FIELDS}
ALL_FILES = {"cmd_config.json"} | {x[0] for x in TEXT_FIELDS + MULTI_FIELDS}
VISION_CAPTION = ("cmd_config.json", ("provider_settings", "default_image_caption_provider_id"), "图片描述", "id")
PLUGIN_OF = {
    "cmd_config.json": None,
    "astrbot_plugin_humanoid_core_config.json": "astrbot_plugin_humanoid_core",
    "astrbot_plugin_livingmemory_config.json": "astrbot_plugin_livingmemory",
    "astrbot_plugin_autonomous_social_config.json": "astrbot_plugin_autonomous_social",
    "astrbot_plugin_clonetts_config.json": "astrbot_plugin_clonetts",
    "meme_manager_config.json": "meme_manager",
    "astrbot_plugin_stealer_config.json": "astrbot_plugin_stealer",
    "astrbot_plugin_group_chat_plus_config.json": "astrbot_plugin_group_chat_plus",
    "astrbot_plugin_community_radar_config.json": "astrbot_plugin_community_radar",
    "astrbot_plugin_memory_diary_config.json": "astrbot_plugin_memory_diary",
}


def g(data: Any, path: tuple, default: Any = None) -> Any:
    cur = data
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def s(data: dict, path: tuple, value: Any) -> None:
    cur = data
    for key in path[:-1]:
        nxt = cur.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[key] = nxt
        cur = nxt
    cur[path[-1]] = value


def empty(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip()) or (isinstance(value, (list, tuple, dict)) and not value)


def short(value: Any, limit: int = 48) -> str:
    text = json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def ctx_text(n: int) -> str:
    if not n:
        return "未知"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    return f"{round(n / 1_000)}K" if n >= 1_000 else str(n)


def read_json(path: str) -> dict:
    with open(path, encoding="utf-8-sig") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{os.path.basename(path)} 不是 JSON 对象")
    return data


def write_json(path: str, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=f".{os.path.basename(path)}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


class ModelSwitchPlugin(Star):
    plugin_id = PLUGIN_ID

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.config = config or {}
        self._lock = asyncio.Lock()
        self._config_error = ""
        self._fallback_notice = ""
        self._refresh_task: asyncio.Task | None = None

    def _path(self, name: str) -> str:
        base = os.path.dirname(os.path.abspath(self.context.get_config().config_path))
        return os.path.join(base, name) if name == "cmd_config.json" else os.path.join(base, "config", name)

    def _catalog(self) -> list[dict]:
        raw = self.context.get_config().get("provider", [])
        by_id = {x["id"]: x for x in raw if isinstance(x, dict) and x.get("id")} if isinstance(raw, list) else {}
        order = {pid: i for i, pid in enumerate(by_id)}
        out = []
        for pid, inst in getattr(self.context.provider_manager, "inst_map", {}).items():
            try:
                meta = inst.meta()
            except Exception:
                continue
            if meta.provider_type != ProviderType.CHAT_COMPLETION:
                continue
            conf = by_id.get(pid, {})
            if conf and conf.get("enable") is False:
                continue
            model = str(meta.model or conf.get("model") or pid)
            # AstrBot 重命名 provider 源时只改 provider_source_id，不会改模型 id，
            # 于是会出现 id=old-vendor/xxx、source=新名称 这种前缀对不上的情况。
            source = str(conf.get("provider_source_id") or "")
            if not source and "/" in pid:
                source = pid.split("/", 1)[0]
            prefix = pid.split("/", 1)[0] if "/" in pid else ""
            mismatch = bool(source and prefix and prefix != source)
            out.append({"id": pid, "model": model, "source": source, "mismatch": mismatch,
                        "modalities": list(conf.get("modalities") or []),
                        "ctx": int(conf.get("max_context_tokens") or 0)})
        out.sort(key=lambda x: order.get(x["id"], 10**6))
        return out

    def _current(self) -> str:
        inst = getattr(self.context.provider_manager, "curr_provider_inst", None)
        if inst is not None:
            try:
                return str(inst.meta().id)
            except Exception:
                pass
        return str(g(self.context.get_config(), PRIMARY, "") or "")

    def _plugin_state(self, file: str) -> str:
        root = PLUGIN_OF.get(file)
        if root is None:
            return "active"
        for meta in star_map.values():
            if getattr(meta, "root_dir_name", None) == root:
                if not getattr(meta, "activated", False) or getattr(meta, "star_cls", None) is None:
                    return "disabled"
                return "active"
        return "missing"

    def _vision_fallback(self) -> str:
        pid = str(self.config.get("vision_fallback_provider_id", "") or "").strip()
        if not pid:
            return ""
        return pid if any(p["id"] == pid and "image" in p["modalities"] for p in self._catalog()) else ""

    def _owner(self, event: AstrMessageEvent) -> bool:
        try:
            sender = str(event.get_sender_id() or "")
            raw = str(self.config.get("owner_ids", "") or "").replace("，", ",")
            owners = [x.strip() for x in raw.split(",") if x.strip()]
            if owners:
                return bool(sender) and sender in owners
            admins = [str(x) for x in (self.context.get_config().get("admins_id") or [])]
            return bool(sender and sender in admins) or bool(event.is_admin())
        except Exception:
            return False

    def _fallbacks(self, current: list, target: str, *bases: str) -> list[str]:
        valid = [p["id"] for p in self._catalog() if p["id"] != target]
        out = []
        for pid in [*bases, *(current or [])]:
            if isinstance(pid, str) and pid in valid and pid not in out:
                out.append(pid)
        return (out or valid[:1])[:2]

    def _configured_fallbacks(self) -> list[str]:
        """插件配置里的备用列表；空列表表示「自动推导」。"""
        raw = self.config.get("fallback_provider_ids") or []
        if isinstance(raw, str):
            raw = [x for x in re.split(r"[,，\s]+", raw) if x]
        out: list[str] = []
        for item in raw if isinstance(raw, list) else []:
            pid = str(item or "").strip()
            if pid and pid not in out:
                out.append(pid)
        return out

    def _pick_fallbacks(self, ids: list[str], target: str) -> list[str]:
        """按配置顺序过滤：必须存在、已启用、且不等于主模型。"""
        valid = {p["id"] for p in self._catalog()}
        out = []
        for pid in ids:
            if pid in valid and pid != target and pid not in out:
                out.append(pid)
        return out[:3]

    def _runtime_fallbacks(self) -> list[str] | None:
        """只读扫描进程对象，拿 pipeline stage 里真正生效的备用列表。

        拿不到就返回 None，不影响其他功能。
        """
        try:
            import gc
        except Exception:
            return None
        try:
            objs = gc.get_objects()
        except Exception:
            return None

        def ids_of(cfg) -> list[str] | None:
            try:
                ids = getattr(cfg, "fallback_provider_ids", None)
            except Exception:
                return None
            if isinstance(ids, list):
                return [str(x) for x in ids]
            return None

        # 1) pipeline stage：ProcessStage -> AgentRequestSubStage -> InternalAgentSubStage
        for obj in objs:
            try:
                if type(obj).__name__ != "InternalAgentSubStage":
                    continue
            except Exception:
                continue
            try:
                got = ids_of(getattr(obj, "main_agent_cfg", None))
            except Exception:
                got = None
            if got is not None:
                return got

        # 2) 兜底：直接找 MainAgentBuildConfig 实例
        for obj in objs:
            try:
                if type(obj).__name__ != "MainAgentBuildConfig":
                    continue
            except Exception:
                continue
            got = ids_of(obj)
            if got is not None:
                return got
        return None

    def _stage_probe(self) -> str:
        """调试用：报告进程内 pipeline stage 的扫描情况。"""
        try:
            import gc
            objs = gc.get_objects()
        except Exception as e:
            return f"gc 扫描失败：{type(e).__name__}: {e}"
        counts: dict[str, int] = {}
        have_cfg = 0
        for obj in objs:
            try:
                name = type(obj).__name__
            except Exception:
                continue
            if name in ("InternalAgentSubStage", "AgentRequestSubStage", "ProcessStage", "MainAgentBuildConfig"):
                counts[name] = counts.get(name, 0) + 1
                if getattr(obj, "main_agent_cfg", None) is not None:
                    have_cfg += 1
        return f"对象总数 {len(objs)}；" + "、".join(f"{k}={v}" for k, v in counts.items()) + f"；带 main_agent_cfg={have_cfg}"

    def _fallback_health(self) -> dict:
        cfg_ids = g(self.context.get_config(), FALLBACK, []) or []
        cfg_ids = [str(x) for x in cfg_ids if isinstance(x, str)]
        inst_map = getattr(self.context.provider_manager, "inst_map", {}) or {}
        return {
            "configured": cfg_ids,
            "runtime": self._runtime_fallbacks(),
            "bad": [x for x in cfg_ids if x not in inst_map],
            "ok": len([x for x in cfg_ids if x in inst_map]),
            "total": len(cfg_ids),
            "fixed": bool(self._configured_fallbacks()),
        }

    def _fmt_fb(self, ids: list[str], cat: dict) -> str:
        if not ids:
            return "未配置"
        return "  ".join(f"{i}. {self._name(pid, cat)}" for i, pid in enumerate(ids, 1))

    def _plan(self, data: dict, target: dict, old_runtime: str) -> tuple[list[dict], list[str], list[str]]:
        cmd = data.get("cmd_config.json")
        if not isinstance(cmd, dict):
            raise RuntimeError("cmd_config.json 不存在或无法解析")
        tid, model, changes = target["id"], target["model"], []

        def add(name, path, label, old, new):
            if old != new:
                changes.append({"file": name, "path": path, "label": label, "old": copy.deepcopy(old), "new": copy.deepcopy(new)})

        old_primary = g(cmd, PRIMARY)
        add("cmd_config.json", PRIMARY, "主对话链（agent_runner + 运行时）", old_primary, tid)
        fb = g(cmd, FALLBACK, [])
        configured = self._configured_fallbacks()
        if configured:
            new_fb = self._pick_fallbacks(configured, tid)
            fb_label = "备用链（按插件配置）"
        else:
            new_fb = self._fallbacks(fb if isinstance(fb, list) else [], tid, str(old_primary or ""), old_runtime)
            fb_label = "备用链（自动）"
        if isinstance(fb, list) or new_fb:
            add("cmd_config.json", FALLBACK, f"{fb_label}（fallback_provider_ids）", fb, new_fb)

        skipped, notes = [], []
        if configured and not new_fb:
            notes.append("配置的备用模型都不可用（不存在或已停用），本次跳过")
        for spec in TEXT_FIELDS:
            name, path, label, kind = spec
            if data.get(name) is None or self._plugin_state(name) != "active":
                continue
            old = g(data[name], path)
            desired = [] if kind == "list" else ""
            if old != desired:
                add(name, path, f"{label}（跟随全局）", old, desired)

        for spec in MULTI_FIELDS:
            name, path, label, kind = spec
            if data.get(name) is None or self._plugin_state(name) != "active":
                continue
            old = g(data[name], path)
            if spec == VISION_CAPTION:
                if "image" in target["modalities"]:
                    if not empty(old):
                        add(name, path, "图片描述（跟随主模型）", old, "")
                else:
                    fallback = self._vision_fallback()
                    if fallback:
                        if old != fallback:
                            add(name, path, "图片描述（视觉备用）", old, fallback)
                    elif not empty(old):
                        notes.append("图片描述保留旧视觉模型（主模型不支持图片）")
                    else:
                        notes.append("图片描述将停用（主模型不支持图片且未配置视觉备用）")
                continue
            if "image" in target["modalities"]:
                if empty(old):
                    continue
                expected = model if kind == "model" else tid
                if old != expected:
                    add(name, path, label, old, expected)
            elif not empty(old):
                skipped.append(label)
        return changes, skipped, notes

    def _plan_follow(self, data: dict, target: dict) -> tuple[list[dict], list[str]]:
        cmd = data.get("cmd_config.json")
        if not isinstance(cmd, dict):
            raise RuntimeError("cmd_config.json 不存在或无法解析")
        changes, notes = [], []

        def add(name, path, label, old, new):
            if old != new:
                changes.append({"file": name, "path": path, "label": label, "old": copy.deepcopy(old), "new": copy.deepcopy(new)})

        for spec in TEXT_FIELDS:
            name, path, label, kind = spec
            state = self._plugin_state(name)
            if data.get(name) is None or state == "missing":
                continue
            old = g(data[name], path)
            desired = [] if kind == "list" else ""
            suffix = "（跟随全局；未启用预清空）" if state == "disabled" else "（跟随全局）"
            if old != desired:
                add(name, path, f"{label}{suffix}", old, desired)

        old = g(cmd, VISION_CAPTION[1])
        if "image" in target["modalities"]:
            if not empty(old):
                add("cmd_config.json", VISION_CAPTION[1], "图片描述（跟随主模型）", old, "")
        else:
            fallback = self._vision_fallback()
            if fallback:
                if old != fallback:
                    add("cmd_config.json", VISION_CAPTION[1], "图片描述（视觉备用）", old, fallback)
            elif not empty(old):
                notes.append("图片描述保留旧视觉模型（主模型不支持图片）")
            else:
                notes.append("图片描述将停用（主模型不支持图片且未配置视觉备用）")
        return changes, notes

    def _cfg_obj(self, name: str):
        if name == "cmd_config.json":
            return self.context.get_config()
        root = name[:-5]
        root = root[: -len("_config")] if root.endswith("_config") else root
        for meta in star_map.values():
            if getattr(meta, "root_dir_name", None) == root and getattr(meta, "config", None) is not None:
                return meta.config
        return None

    def _memory(self, changes: list[dict], reverse: bool = False) -> None:
        for ch in reversed(changes) if reverse else changes:
            obj = self._cfg_obj(ch["file"])
            if obj is not None:
                try:
                    s(obj, ch["path"], copy.deepcopy(ch["old"] if reverse else ch["new"]))
                except Exception as e:
                    self.logger.warning("同步内存配置失败 %s: %s", ch["file"], e)

    def _load_all(self) -> dict[str, dict | None]:
        out = {}
        for name in ALL_FILES:
            try:
                out[name] = read_json(self._path(name))
            except Exception as e:
                self.logger.warning("读取 %s 失败: %s", name, e)
                out[name] = None
        return out

    def _backup(self, path: str, stamp: str) -> str:
        bak, n = f"{path}.bak-{stamp}", 1
        while os.path.exists(bak):
            bak, n = f"{path}.bak-{stamp}-{n}", n + 1
        shutil.copy2(path, bak)
        return bak

    def _restore(self, backups: list[tuple[str, str]]) -> list[str]:
        failed = []
        for path, bak in backups:
            try:
                shutil.copy2(bak, path)
            except Exception as e:
                self.logger.error("回滚 %s 失败: %s", path, e)
                failed.append(os.path.basename(path))
        return failed

    def _verify(self, tid: str, changes: list[dict], backups: list[tuple[str, str]], check_provider: bool = True) -> None:
        if check_provider:
            inst = getattr(self.context.provider_manager, "curr_provider_inst", None)
            if inst is None or str(inst.meta().id) != tid:
                raise RuntimeError("运行时 provider 读回不一致")
            if str(g(self.context.get_config(), PRIMARY, "")) != tid:
                raise RuntimeError("agent_runner 配置读回不一致")
        for ch in changes:
            if g(read_json(self._path(ch["file"])), ch["path"]) != ch["new"]:
                raise RuntimeError(f"{ch['file']} 字段读回不一致：{'.'.join(ch['path'])}")
        for _, bak in backups:
            if not os.path.exists(bak):
                raise RuntimeError(f"备份读回不存在：{os.path.basename(bak)}")

    @staticmethod
    def _label(p: dict | None) -> str:
        """给人看的名字：优先「源名称 · 模型名」，而不是模型 id。"""
        if not p:
            return ""
        source = str(p.get("source") or "").strip()
        model = str(p.get("model") or "").strip() or str(p.get("id") or "")
        if source and model and source != model:
            return f"{source} · {model}"
        return model or source or str(p.get("id") or "")

    def _name(self, pid: str, cat: dict | None = None) -> str:
        return self._label((cat or {}).get(pid)) or pid

    def _fb_text(self) -> str:
        cat = {p["id"]: p for p in self._catalog()}
        ids = g(self.context.get_config(), FALLBACK, []) or []
        return "、".join(self._name(x, cat) for x in ids if isinstance(x, str)) or "无"

    def _list_text(self) -> str:
        items = self._catalog()
        if not items:
            return "没有可用的对话模型（provider 未启用或未加载）。"
        cur = self._current()
        lines = [f"可用对话模型（{len(items)} 个）："]
        for i, p in enumerate(items, 1):
            tool = "?" if not p["modalities"] else ("✓" if "tool_use" in p["modalities"] else "✗")
            image = "?" if not p["modalities"] else ("✓" if "image" in p["modalities"] else "✗")
            lines.append(f"{i}. {self._label(p)}  tool{tool} 图{image}  ctx {ctx_text(p['ctx'])}{'  ← 当前' if p['id'] == cur else ''}")
        wrong = [p for p in items if p.get("mismatch")]
        if wrong:
            lines.append("")
            lines.append("⚠️ 以下模型的 id 前缀与 UI 里的源名称不一致（AstrBot 重命名源后不会改模型 id，切换仍按 id 生效）：")
            for p in wrong:
                lines.append(f"  · {p['source']} → id {p['id']}")
        lines.append("回复 /切换主模型 编号 切换；/切换主模型 状态 体检；/切换主模型 跟随 改为跟随主模型；/切换主模型 测试 真发最小请求")
        return "\n".join(lines)

    def _resolve(self, token: str) -> dict:
        items = self._catalog()
        if token.isdigit():
            idx = int(token)
            if 1 <= idx <= len(items):
                return items[idx - 1]
            raise ValueError(f"编号超出范围（1-{len(items)}）")
        exact = [p for p in items if p["id"] == token]
        if exact:
            return exact[0]
        low = token.lower()
        fuzzy = [
            p for p in items
            if p["model"] == token
            or low in p["model"].lower()
            or low in p["id"].lower()
            or low in str(p.get("source") or "").lower()
        ]
        if len(fuzzy) == 1:
            return fuzzy[0]
        if not fuzzy:
            raise ValueError(f"找不到模型：{token}")
        raise ValueError("匹配到多个模型：" + "、".join(self._label(p) for p in fuzzy))

    async def _apply_changes(self, data: dict, changes: list[dict], *, switch_to: str | None, old_runtime: str, verify_runtime: bool) -> list[tuple[str, str]]:
        files = sorted({ch["file"] for ch in changes})
        if any(data.get(name) is None for name in files):
            raise RuntimeError("部分配置文件读取失败，未做任何修改")
        backups, stamp = [], datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            for name in files:
                backups.append((self._path(name), self._backup(self._path(name), stamp)))
        except Exception as e:
            for _, bak in backups:
                try:
                    os.remove(bak)
                except OSError:
                    pass
            raise RuntimeError(f"备份失败，未做任何修改：{e}")

        try:
            for name in files:
                new_data = copy.deepcopy(data[name])
                for ch in changes:
                    if ch["file"] == name:
                        s(new_data, ch["path"], copy.deepcopy(ch["new"]))
                write_json(self._path(name), new_data)
        except Exception as e:
            failed = self._restore(backups)
            raise RuntimeError(f"写入失败{'；回滚不完整：' + '、'.join(failed) if failed else '；已回滚'}：{e}\n备份：{'、'.join(os.path.basename(b) for _, b in backups)}")

        try:
            if switch_to:
                inst = getattr(self.context.provider_manager, "curr_provider_inst", None)
                if inst is None or str(inst.meta().id) != switch_to:
                    await self.context.provider_manager.set_provider(switch_to, ProviderType.CHAT_COMPLETION)
            self._memory(changes)
            self._verify(switch_to or old_runtime, changes, backups, check_provider=verify_runtime)
        except Exception as e:
            failed = self._restore(backups)
            self._memory(changes, reverse=True)
            if switch_to and old_runtime and old_runtime != switch_to:
                try:
                    await self.context.provider_manager.set_provider(old_runtime, ProviderType.CHAT_COMPLETION)
                except Exception:
                    pass
            raise RuntimeError(f"校验失败{'；回滚不完整：' + '、'.join(failed) if failed else '；已回滚'}：{e}\n备份：{'、'.join(os.path.basename(b) for _, b in backups)}")
        return backups

    async def _switch(self, target: dict, operator: str) -> str:
        data = self._load_all()
        if not isinstance(data.get("cmd_config.json"), dict):
            raise RuntimeError("cmd_config.json 不存在或无法解析")
        tid, model = target["id"], target["model"]
        old_runtime = self._current()
        old_primary = str(g(data["cmd_config.json"], PRIMARY, "") or "")
        changes, skipped, notes = self._plan(data, target, old_runtime)
        same = old_runtime == tid and old_primary == tid
        cur_line = f"当前：{model}  tool{'✓' if 'tool_use' in target['modalities'] else '✗'} 图{'✓' if 'image' in target['modalities'] else '✗'}  ctx {ctx_text(target['ctx'])}"
        if not changes:
            warn = "\n⚠️ 目标不支持 tool_use，生图/记忆工具会失效" if "tool_use" not in target["modalities"] else ""
            extra = ("\n" + "\n".join(f"· {n}" for n in notes)) if notes else ""
            return f"ℹ️ 已经是该模型：{model}\n配置无漂移，无需改动。{warn}{extra}\n\n{cur_line}\n备用：{self._fb_text()}"

        backups = await self._apply_changes(data, changes, switch_to=tid, old_runtime=old_runtime, verify_runtime=True)
        try:
            await self.put_kv_data("last_switch", {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "operator": operator,
                                                   "from": old_runtime, "to": tid, "backups": [os.path.basename(b) for _, b in backups]})
        except Exception as e:
            self.logger.warning("记录切换状态失败: %s", e)

        lines = [f"ℹ️ 已经是该模型：{model}，已顺手修复漂移" if same else f"✅ 已切换到 {model}"]
        if not same:
            lines.append("生效：全局（私聊+群聊）｜立即生效，无需重启")
        if "tool_use" not in target["modalities"]:
            lines.append("⚠️ 目标不支持 tool_use，生图/记忆工具会失效")
        lines += ["", f"{'修复' if same else '改动'} {len(changes)} 项"] + [f"· {ch['label']}" for ch in changes]
        skip_items = ([f"多模态 ×{len(skipped)}（目标不支持图片：{'、'.join(skipped)}）"] if skipped else []) + ["embedding（专用模型，不动）"]
        lines += ["", f"跳过 {len(skipped) + 1} 项"] + [f"· {x}" for x in skip_items]
        if notes:
            lines += ["", "提示"] + [f"· {n}" for n in notes]
        return "\n".join(lines + ["", cur_line, f"备用：{self._fb_text()}"])

    async def _follow(self, operator: str) -> str:
        data = self._load_all()
        if not isinstance(data.get("cmd_config.json"), dict):
            raise RuntimeError("cmd_config.json 不存在或无法解析")
        cur = self._current()
        target = next((p for p in self._catalog() if p["id"] == cur), {"id": cur, "model": cur, "modalities": [], "ctx": 0})
        changes, notes = self._plan_follow(data, target)
        mods = target["modalities"]
        tool = "?" if not mods else ("✓" if "tool_use" in mods else "✗")
        image = "?" if not mods else ("✓" if "image" in mods else "✗")
        cur_line = f"当前：{target['model']}  tool{tool} 图{image}  ctx {ctx_text(target['ctx'])}"
        if not changes:
            extra = ("\n" + "\n".join(f"· {n}" for n in notes)) if notes else ""
            return f"ℹ️ 已经是跟随模式，无需改动。{extra}\n\n{cur_line}\n备用：{self._fb_text()}"
        backups = await self._apply_changes(data, changes, switch_to=None, old_runtime=cur, verify_runtime=False)
        try:
            await self.put_kv_data("last_follow", {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "operator": operator,
                                                   "backups": [os.path.basename(b) for _, b in backups]})
        except Exception as e:
            self.logger.warning("记录跟随迁移状态失败: %s", e)
        lines = ["✅ 已改为跟随主模型", "", f"改动 {len(changes)} 项"] + [f"· {ch['label']}" for ch in changes]
        if notes:
            lines += ["", "提示"] + [f"· {n}" for n in notes]
        return "\n".join(lines + ["", cur_line, f"备用：{self._fb_text()}"])

    async def _status(self) -> str:
        cfg, data = self.context.get_config(), self._load_all()
        cat = {p["id"]: p for p in self._catalog()}
        cur = self._current()
        cur_info = cat.get(cur, {})
        image_ok = "image" in cur_info.get("modalities", [])
        raw_fallback = str(self.config.get("vision_fallback_provider_id", "") or "").strip()
        fallback = self._vision_fallback()
        follow, drift, pinned, held = 0, [], [], []
        disabled_map: dict[str, list[tuple[str, Any]]] = {}

        for spec in TEXT_FIELDS:
            name, path, label, kind = spec
            if data.get(name) is None:
                continue
            state = self._plugin_state(name)
            value = g(data[name], path)
            if state == "disabled":
                disabled_map.setdefault(name, []).append((label, value))
                continue
            if state == "missing":
                continue
            if empty(value):
                follow += 1
                continue
            ok = value == [cur] if kind == "list" else value == cur
            if ok:
                pinned.append(label)
            else:
                drift.append(f"{label}={short(value)}（建议 /切换主模型 跟随）")

        for spec in MULTI_FIELDS:
            name, path, label, kind = spec
            if data.get(name) is None:
                continue
            state = self._plugin_state(name)
            value = g(data[name], path)
            if state == "disabled":
                if not empty(value):
                    disabled_map.setdefault(name, []).append((label, value))
                continue
            if state == "missing":
                continue
            if spec == VISION_CAPTION:
                if image_ok:
                    if not empty(value):
                        drift.append(f"{label}={short(value)}（建议 /切换主模型 跟随）")
                elif fallback:
                    if value != fallback:
                        drift.append(f"{label}={short(value)}（建议 /切换主模型 跟随）")
                elif not empty(value):
                    held.append(label)
                continue
            if empty(value):
                continue
            if not image_ok:
                held.append(label)
                continue
            expected = cur_info.get("model") or cur
            ok = value in (cur, expected) if kind == "model" else value == cur
            if not ok:
                drift.append(f"{label}={short(value)}")

        if str(g(cfg, PRIMARY, "") or "") != cur:
            drift.insert(0, f"agent_runner={short(g(cfg, PRIMARY, '') or '空')}")

        caption_value = g(data.get("cmd_config.json") or {}, VISION_CAPTION[1])
        if image_ok:
            vision_line = "图片描述：跟随主模型（主模型支持图片）" if empty(caption_value) else "图片描述：仍写死旧模型（建议 /切换主模型 跟随）"
        elif fallback:
            vision_line = "图片描述：使用视觉备用" if caption_value == fallback else "图片描述：未使用视觉备用（建议 /切换主模型 跟随）"
        elif not empty(caption_value):
            vision_line = "图片描述：视觉保留旧模型（主模型无图）"
        else:
            vision_line = "图片描述：停用（主模型无图，未配置视觉备用）"

        disabled_lines = []
        for name, fields in disabled_map.items():
            root = PLUGIN_OF.get(name) or name
            short_name = root.replace("astrbot_plugin_", "")
            has_value = any(not empty(v) for _, v in fields)
            if name in TEXT_FILES:
                disabled_lines.append(
                    f"{short_name}（可用 /切换主模型 跟随 清空）" if has_value
                    else f"{short_name}（已清空，重新启用后跟随全局）"
                )
            elif has_value:
                disabled_lines.append(f"{short_name}（忽略模型配置）")

        state, follow_state = {}, {}
        try:
            state = await self.get_kv_data("last_switch", {}) or {}
            follow_state = await self.get_kv_data("last_follow", {}) or {}
        except Exception:
            pass
        state = state if isinstance(state, dict) else {}
        follow_state = follow_state if isinstance(follow_state, dict) else {}

        cur_label = self._name(cur, cat) if cur else "未设置"
        cur_line = f"{cur_label}（{cur}）" if cur and cur_label != cur else cur_label
        health = self._fallback_health()
        mode = "插件固定" if health["fixed"] else "自动"
        lines = ["📊 模型切换状态", f"当前全局：{cur_line}",
                 f"备用链（{mode}）：{self._fmt_fb(health['configured'], cat)}"]
        run_ids = health["runtime"]
        if run_ids is None:
            lines.append("运行中的备用链：读取失败（建议重启后确认）")
        else:
            same = run_ids == health["configured"]
            lines.append(f"运行中的备用链：{self._fmt_fb(run_ids, cat)}" + ("" if same else "  ← 与配置不一致"))
            if not same:
                lines.append("  ⚠️ 运行中是启动时加载的旧值，重启 AstrBot 后才会用上面的配置")
        bad = health["bad"]
        lines.append(f"备用链可解析：{health['ok']}/{health['total']}" + (f"（失效：{'、'.join(bad)}）" if bad else ""))
        lines.append(f"跟随全局：{follow} 项（仅启用插件的后台任务）")
        if drift:
            lines += [f"写死未跟随：{len(drift)} 项 ⚠️"] + [f"  · {x}" for x in drift[:8]]
            if len(drift) > 8:
                lines.append(f"  · …等 {len(drift)} 项")
        else:
            lines.append("写死未跟随：0 项 ✅")
        if pinned:
            lines.append(f"写死但一致：{len(pinned)} 项（{'、'.join(pinned)}；建议 /切换主模型 跟随）")
        lines.append(vision_line)
        if held:
            lines.append(f"多模态保留：{len(held)} 项（主模型无图）")
        if disabled_lines:
            lines.append("未启用：" + "；".join(disabled_lines))
        lines.append(f"视觉备用：{raw_fallback}（不可用或不支持图片）" if raw_fallback and not fallback else f"视觉备用：{self._name(fallback, cat) if fallback else '未配置'}")
        lines.append("不可切换：embedding（openai_embedding）｜STT/TTS｜rerank｜omnidraw 出图模型（固定，提示词副脑跟随主模型）｜qq_tools 视频模型")
        wrong = [p for p in self._catalog() if p.get("mismatch")]
        if wrong:
            lines.append("⚠️ id 与 UI 名称不一致：" + "；".join(f"{p['source']} → {p['id']}" for p in wrong))
        lines.append(f"最后切换：{state.get('time', '未知')} · {state.get('operator', '未知')}" if state else "最后切换：无记录")
        if follow_state:
            lines.append(f"最后跟随迁移：{follow_state.get('time', '未知')} · {follow_state.get('operator', '未知')}")
        return "\n".join(lines)

    async def _info_snapshot(self) -> dict[str, str]:
        cur = self._current()
        cat = {p["id"]: p for p in self._catalog()}
        status = await self._status()
        last = "无记录"
        try:
            st = await self.get_kv_data("last_switch", {}) or {}
            lf = await self.get_kv_data("last_follow", {}) or {}
            parts = []
            if isinstance(st, dict) and st:
                parts.append(f"切换 {st.get('time', '?')} · {st.get('operator', '?')}")
            if isinstance(lf, dict) and lf:
                parts.append(f"跟随迁移 {lf.get('time', '?')} · {lf.get('operator', '?')}")
            last = "；".join(parts) or "无记录"
        except Exception:
            pass
        bits = []
        if self._config_error:
            bits.append(f"⚠️ {self._config_error}")
        if self._fallback_notice:
            bits.append(f"ℹ️ {self._fallback_notice}")
        summary = "\n\n".join(bits + [status]) if bits else status
        return {
            "current_model_info": f"{self._name(cur, cat)}（{cur}）" if cur else "未设置",
            "fallback_model_info": self._fb_text(),
            "drift_summary": summary,
            "last_switch_info": last,
        }

    async def _refresh_info(self, switch_value: str | None = None) -> None:
        try:
            values = await self._info_snapshot()
        except Exception as e:
            self.logger.warning("生成模型配置信息失败: %s", e)
            return
        changed = False
        for key, value in values.items():
            if self.config.get(key) != value:
                self.config[key] = value
                changed = True
        if switch_value is not None and self.config.get("switch_provider_id") != switch_value:
            self.config["switch_provider_id"] = switch_value
            changed = True
        if changed and hasattr(self.config, "save_config"):
            try:
                self.config.save_config()
            except Exception as e:
                self.logger.warning("保存模型配置信息失败: %s", e)

    async def initialize(self) -> None:
        self._start_auto_refresh()
        try:
            if not self._catalog():
                await self._refresh_info()
                return
            requested = str(self.config.get("switch_provider_id", "") or "").strip()
            cur = self._current()
            if requested and requested != cur:
                target = next((p for p in self._catalog() if p["id"] == requested), None)
                if target is None:
                    self._config_error = f"配置的模型 {requested} 不存在或未启用，已保留当前模型。"
                else:
                    try:
                        await self._switch(target, "WebUI")
                        self._config_error = ""
                    except Exception as e:
                        self._config_error = f"从配置页切换失败：{e}"
                        self.logger.warning("配置页切换模型失败: %s", e)
                cur = self._current()
            await self._sync_fallback(cur)
            await self._refresh_info(switch_value=cur)
        except Exception as e:
            self.logger.warning("初始化模型配置信息失败: %s", e)

    REFRESH_INTERVAL = 120.0

    def _start_auto_refresh(self) -> None:
        if self._refresh_task and not self._refresh_task.done():
            return
        try:
            self._refresh_task = asyncio.create_task(self._auto_refresh_loop())
        except RuntimeError:
            # 没有事件循环时（例如被外部单测直接构造）静默跳过
            self._refresh_task = None

    async def _auto_refresh_loop(self) -> None:
        """定时重算配置页快照，让 UI 增删 provider 后页面能自己跟上。"""
        while True:
            await asyncio.sleep(self.REFRESH_INTERVAL)
            try:
                await self._refresh_info()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.logger.debug("自动刷新模型配置信息失败: %s", e)

    async def terminate(self) -> None:
        task, self._refresh_task = self._refresh_task, None
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _sync_fallback(self, cur: str, *, operator: str = "插件配置") -> str:
        """把插件配置里的备用列表写进 cmd_config；返回提示文案。"""
        configured = self._configured_fallbacks()
        if not configured:
            return ""
        data = self._load_all()
        cmd = data.get("cmd_config.json")
        if not isinstance(cmd, dict):
            self._fallback_notice = "读取 cmd_config.json 失败，备用链未写入"
            return self._fallback_notice
        current = g(cmd, FALLBACK, [])
        target = self._pick_fallbacks(configured, cur)
        if not target:
            self._fallback_notice = "配置的备用模型都不可用（不存在或已停用），未写入"
            return self._fallback_notice
        if list(current or []) == target:
            return ""
        changes = [{
            "file": "cmd_config.json",
            "path": FALLBACK,
            "label": "备用链（按插件配置）",
            "old": current,
            "new": target,
        }]
        try:
            await self._apply_changes(data, changes, switch_to=None, old_runtime=cur, verify_runtime=False)
        except Exception as e:
            self._fallback_notice = f"写入备用链失败：{e}"
            return self._fallback_notice
        self._fallback_notice = "备用链已按配置写入，重启 AstrBot 后生效"
        try:
            await self.put_kv_data("last_fallback", {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "operator": operator,
                "ids": target,
            })
        except Exception:
            pass
        return self._fallback_notice

    async def _test_fallbacks(self, ids: list[str]) -> str:
        if not ids:
            return "没有配置备用模型。"
        cat = {p["id"]: p for p in self._catalog()}
        inst_map = getattr(self.context.provider_manager, "inst_map", {}) or {}
        lines = ["🧪 备用链测试"]
        for i, pid in enumerate(ids, 1):
            name = self._name(pid, cat)
            prov = inst_map.get(pid)
            if prov is None:
                lines.append(f"  {i}. {name}  ❌ provider 不存在")
                continue
            t0 = time.monotonic()
            try:
                resp = await asyncio.wait_for(
                    prov.text_chat(prompt="ping", system_prompt="Reply with OK only.", request_max_retries=0),
                    timeout=30,
                )
                txt = short(getattr(resp, "completion_text", "") or "", 30)
                lines.append(f"  {i}. {name}  ✅ {txt or 'OK'}  {time.monotonic() - t0:.1f}s")
            except Exception as e:
                lines.append(f"  {i}. {name}  ❌ {type(e).__name__}: {short(str(e), 80)}")
        return "\n".join(lines)

    async def _cmd_fallback(self, args: list[str], operator: str) -> str:
        cat = {p["id"]: p for p in self._catalog()}
        h = self._fallback_health()
        if not args:
            run = h["runtime"]
            lines = ["🔁 备用链",
                     f"插件配置：{self._fmt_fb(h['configured'], cat)}",
                     "运行中：" + ("读取失败（建议重启后确认）" if run is None else self._fmt_fb(run, cat)),
                     f"可解析：{h['ok']}/{h['total']}" + (f"（失效：{'、'.join(h['bad'])}）" if h["bad"] else ""),
                     "",
                     "用法：",
                     "  /切换主模型 备用 2 3     按顺序设为第 2、3 个模型",
                     "  /切换主模型 备用 自动    恢复自动推导",
                     "  /切换主模型 备用 测试    逐个真发 ping",
                     "  /切换主模型 备用 调试    查看运行中列表的读取情况"]
            return "\n".join(lines)

        head = args[0]
        if head in ("测试", "test"):
            return await self._test_fallbacks(h["configured"] or (h["runtime"] or []))

        if head in ("调试", "debug"):
            return "🔍 " + self._stage_probe()

        if head in ("自动", "auto"):
            self.config["fallback_provider_ids"] = []
            if hasattr(self.config, "save_config"):
                try:
                    self.config.save_config()
                except Exception as e:
                    self.logger.warning("保存备用配置失败: %s", e)
            self._fallback_notice = "已切回自动模式（下次切换时自动推导），当前备用链保持不变"
            await self._refresh_info()
            return "✅ 已切回自动模式。\n" + self._fallback_notice

        ids: list[str] = []
        for token in args:
            try:
                p = self._resolve(token)
            except ValueError as e:
                return f"❌ {e}"
            if p["id"] not in ids:
                ids.append(p["id"])
        if len(ids) > 3:
            return "❌ 最多设置 3 个备用模型。"
        cur = self._current()
        ids = [x for x in ids if x != cur]
        if not ids:
            return "❌ 备用模型不能和主模型相同。"

        self.config["fallback_provider_ids"] = ids
        if hasattr(self.config, "save_config"):
            try:
                self.config.save_config()
            except Exception as e:
                return f"❌ 保存配置失败：{e}"

        note = await self._sync_fallback(cur, operator=operator)
        await self._refresh_info()
        body = "\n".join(f"  {i}. {self._name(pid, cat)}" for i, pid in enumerate(ids, 1))
        return "✅ 已设置备用链：\n" + body + ("\n\n" + note if note else "\n\n配置与当前一致，无需写入。")

    @filter.command("切换主模型")
    async def cmd_model_switch(self, event: AstrMessageEvent) -> AsyncGenerator[Any, None]:
        """切换全局主模型。"""
        if not bool(self.config.get("enabled", True)):
            yield event.plain_result("模型切换插件已停用。")
            return
        if not self._owner(event):
            yield event.plain_result("仅主人可用。")
            return
        raw = str(getattr(event, "message_str", "") or "").strip()
        raw = re.sub(r"\[[^\]]{1,80}\]", " ", raw).strip()
        for prefix in ("/切换主模型", "切换主模型"):
            if raw.startswith(prefix):
                raw = raw[len(prefix):].strip()
                break
        parts = raw.split()
        sub = parts[0] if parts else ""
        if not sub or sub in ("列表", "list"):
            text = self._list_text()
            await self._refresh_info()
            yield event.plain_result(text)
            return
        if sub in ("状态", "status"):
            text = await self._status()
            await self._refresh_info()
            yield event.plain_result(text)
            return
        if self._lock.locked():
            yield event.plain_result("⏳ 正在切换中，请稍候")
            return
        operator = f"{event.get_sender_name()}({event.get_sender_id()})"
        async with self._lock:
            if sub in ("备用", "fallback", "fb"):
                yield event.plain_result(await self._cmd_fallback(parts[1:], operator))
                return
            if sub in ("测试", "test"):
                pid = self._current()
                prov = getattr(self.context.provider_manager, "inst_map", {}).get(pid)
                if prov is None:
                    yield event.plain_result(f"❌ 测试失败：provider {pid or '未设置'} 不存在")
                    return
                try:
                    resp = await asyncio.wait_for(prov.text_chat(prompt="ping", system_prompt="Reply with OK only.", request_max_retries=0), timeout=30)
                    yield event.plain_result(f"✅ 测试成功：{pid} 返回 {short(getattr(resp, 'completion_text', '') or '', 60)}")
                except Exception as e:
                    yield event.plain_result(f"❌ 测试失败：{pid} {type(e).__name__}: {e}")
                return
            if sub in ("跟随", "follow", "迁移"):
                try:
                    text = await self._follow(operator)
                    self._config_error = ""
                    await self._refresh_info(switch_value=self._current())
                    yield event.plain_result(text)
                except Exception as e:
                    self.logger.exception("跟随迁移失败")
                    self._config_error = f"跟随迁移失败：{e}"
                    await self._refresh_info(switch_value=self._current())
                    msg = f"❌ 跟随迁移失败：{e}\n当前仍是 {self._name(self._current())}"
                    if "备份：" not in str(e):
                        msg += "\n备份：无（未写入任何文件）"
                    yield event.plain_result(msg)
                return
            try:
                target = self._resolve(sub)
            except ValueError as e:
                yield event.plain_result(f"❌ {e}\n当前仍是 {self._name(self._current())}\n备份：无（未改动）\n\n{self._list_text()}")
                return
            try:
                text = await self._switch(target, operator)
                self._config_error = ""
                await self._refresh_info(switch_value=self._current())
                yield event.plain_result(text)
            except Exception as e:
                self.logger.exception("切换主模型失败")
                self._config_error = f"切换失败：{e}"
                await self._refresh_info(switch_value=self._current())
                msg = f"❌ 切换失败：{e}\n当前仍是 {self._name(self._current())}"
                if "备份：" not in str(e):
                    msg += "\n备份：无（未写入任何文件）"
                yield event.plain_result(msg)





























