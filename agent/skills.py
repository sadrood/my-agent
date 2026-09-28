"""Skills 技能包机制（技能包式能力扩展，最小可用实现）。"""

import hashlib
import os
import re
import shutil
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# frontmatter 支持的元数据键
_META_KEYS = ("name", "description", "triggers")
# 正文分词使用的分隔符集合（字母数字 + 中文为词）
_WORD_SPLIT_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")


@dataclass
class Skill:
    """一个已加载的技能。"""

    name: str
    description: str = ""
    triggers: List[str] = field(default_factory=list)
    body: str = ""
    path: str = ""
    scripts: List[str] = field(default_factory=list)


class SkillManager:
    """扫描 / 匹配 / 渲染技能。纯逻辑，无全局状态，便于测试。"""

    def __init__(self, project_dir: Optional[str] = "./skills",
                 user_dir: Optional[str] = None,
                 max_chars: int = 6000):
        self.project_dir = project_dir or "./skills"
        self.user_dir = user_dir or os.path.expanduser("~/.my_agent/skills")
        self.max_chars = int(max_chars or 6000)
        self._skills: Optional[List[Skill]] = None

    # ------------------------------------------------------------------
    # 发现
    # ------------------------------------------------------------------
    def discover(self) -> List[Skill]:
        """扫描项目级与用户级技能目录，返回按技能名排序的技能列表（稳定）。"""
        if self._skills is not None:
            return self._skills
        skills: Dict[str, Skill] = {}
        # 先扫用户级、后扫项目级：同名时项目级覆盖（项目级优先）
        for base_dir in (self.user_dir, self.project_dir):
            if not base_dir or not os.path.isdir(base_dir):
                continue
            try:
                entries = sorted(os.listdir(base_dir))
            except OSError:
                continue
            for entry in entries:
                if not self._is_safe_name(entry):
                    continue
                skill_dir = os.path.join(base_dir, entry)
                md_path = os.path.join(skill_dir, "SKILL.md")
                if not os.path.isfile(md_path):
                    continue
                skill = self._load_skill(md_path, entry)
                skill.scripts = self._scan_scripts(skill_dir)
                skills[skill.name] = skill
        self._skills = sorted(skills.values(), key=lambda s: s.name)
        return self._skills

    @staticmethod
    def _is_safe_name(name: str) -> bool:
        """技能名（目录名）必须安全：非空、不含路径分隔符、不含 '..'。"""
        if not name or name in (".", ".."):
            return False
        if ".." in name:
            return False
        if "/" in name or "\\" in name:
            return False
        if os.sep in name:
            return False
        if os.altsep and os.altsep in name:
            return False
        return True

    def _load_skill(self, md_path: str, fallback_name: str) -> Skill:
        """读取 SKILL.md，解析 frontmatter 与正文。任何读取/解析异常都降级。"""
        try:
            with open(md_path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            return Skill(name=fallback_name)
        meta, body_start = self._parse_frontmatter(text)
        lines = text.split("\n")
        body = "\n".join(lines[body_start:]).strip()
        return Skill(
            name=meta.get("name") or fallback_name,
            description=meta.get("description", ""),
            triggers=meta.get("triggers", []),
            body=body,
            path=md_path,
        )

    @staticmethod
    def _scan_scripts(skill_dir: str) -> List[str]:
        """扫描技能目录第一层的普通文件，登记相对路径（按文件名排序）。"""
        scripts: List[str] = []
        try:
            names = sorted(os.listdir(skill_dir))
        except OSError:
            return []
        for name in names:
            if name.startswith("."):
                continue  # 隐藏文件跳过
            if name == "SKILL.md":
                continue  # SKILL.md 自身不登记
            if os.path.isfile(os.path.join(skill_dir, name)):
                scripts.append(name)
        return scripts

    @staticmethod
    def _parse_frontmatter(text: str):
        """极简 frontmatter 解析（支持 YAML folded/literal 块与多行缩进续行）。"""
        lines = text.split("\n")
        if not lines or lines[0].strip() != "---":
            return {}, 0
        meta: Dict[str, object] = {}
        cur_key = None  # 当前文本键（支持多行缩进续行）
        for idx in range(1, len(lines)):
            line = lines[idx]
            if line.strip() == "---":
                # 块指示符落空（description: > 后无内容）→ 置空
                for k in ("name", "description"):
                    if meta.get(k) in (">", "|"):
                        meta[k] = ""
                return meta, idx + 1  # 找到闭合围栏
            # 缩进续行：拼到上一个 name/description（YAML folded/literal 语义简化）
            if line[:1] in (" ", "	") and cur_key in ("name", "description"):
                piece = line.strip()
                if piece:
                    prev = str(meta.get(cur_key, "")).strip()
                    meta[cur_key] = (prev + " " + piece) if prev and prev not in (">", "|") else piece
                continue
            if ":" not in line:
                cur_key = None
                continue  # 损坏行容错：静默跳过
            key, _, value = line.partition(":")
            key = key.strip().lower()
            value = value.strip()
            if key not in _META_KEYS:
                cur_key = None
                continue
            cur_key = key
            if key == "triggers":
                triggers = [t.strip() for t in value.split(",") if t.strip()]
                meta["triggers"] = triggers
            elif value in (">", "|"):
                # YAML 折叠/字面块指示符：内容在后续缩进行，占位等续行拼接
                meta[key] = value
            else:
                meta[key] = value
        # 未找到闭合围栏 → 解析失败，降级为无 frontmatter
        return {}, 0

    def match(self, goal: str) -> List[Skill]:
        """对 goal 做关键词命中判断（name / description / triggers）。"""
        if not goal:
            return []
        goal_lower = goal.lower()
        hits = []
        for skill in self.discover():
            if self._skill_matches(skill, goal_lower):
                hits.append(skill)
        return hits

    def _skill_matches(self, skill: Skill, goal_lower: str) -> bool:
        # 1. triggers 关键词直接包含匹配
        for t in skill.triggers:
            if t and t.lower() in goal_lower:
                return True
        # 2. name / description 分词包含匹配
        for text in (skill.name, skill.description):
            if text and self._text_hits(text, goal_lower):
                return True
        return False

    @staticmethod
    def _text_hits(text: str, goal_lower: str) -> bool:
        text_lower = text.lower()
        # 整段包含（对中文整句也有意义）
        if text_lower in goal_lower:
            return True
        # 分词包含（对英文多词名 / 混合文本有意义）
        for word in _WORD_SPLIT_RE.split(text_lower):
            if len(word) < 2:
                continue
            if not word.isascii():
                # 中日韩没有词边界，整段子串匹配才是对的
                if word in goal_lower:
                    return True
                continue
            # 1~2 字母的英文词（to/in/is/or/it/on/as…）只认**词边界**。
            if len(word) >= 3:
                if word in goal_lower:
                    return True
            elif re.search(rf"\b{re.escape(word)}\b", goal_lower):
                return True
        return False

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------
    def render_for_prompt(self, goal: str) -> str:
        """渲染注入文本：全部技能一句话索引 + 命中技能的完整正文。"""
        skills = self.discover()
        if not skills:
            return ""
        matched = self.match(goal)
        matched_names = {s.name for s in matched}

        parts = ["## 可用技能（Skills）"]
        index_lines = []
        for s in skills:
            line = f"- {s.name}"
            if s.description:
                line += f"：{s.description}"
            if s.scripts:
                # 脚本计数提示：仅告知有多少附带脚本，不自动放行执行
                line += f"（含 {len(s.scripts)} 个脚本）"
            index_lines.append(line)
        index_text = "技能索引：\n" + "\n".join(index_lines)
        # 命中正文优先：先把正文的预算留出来，索引按剩余空间截断。旧实现是"索引 + 正文拼完再统一尾部截断"，而索引用的是**完整 description**。
        if matched and self.max_chars:
            body_budget = sum(len(s.body or "") for s in matched)
            head_room = max(200, self.max_chars - body_budget - 64)
            if len(index_text) > head_room:
                index_text = index_text[:head_room] + "\n…（技能索引已截断）"
        parts.append(index_text)
        if matched:
            body_lines = ["\n命中技能正文："]
            for s in matched:
                body_lines.append(f"\n### 技能: {s.name}")
                body_lines.append(s.body if s.body else "（无正文）")
                if s.scripts:
                    # 附带脚本清单：绝对路径，只作提示，执行仍走审批门
                    body_lines.append("附带脚本：")
                    for rel in s.scripts:
                        body_lines.append(os.path.join(os.path.dirname(s.path), rel))
            parts.append("\n".join(body_lines))
        text = "\n".join(parts)
        if self.max_chars and len(text) > self.max_chars:
            text = text[:self.max_chars] + "\n\n…（技能正文已按 SKILLS_MAX_CHARS 截断，skill 截断）"
        return text

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<SkillManager project={self.project_dir!r} "
                f"user={self.user_dir!r} max_chars={self.max_chars}>")


# ======================================================================
# 技能包安装 / 更新（带完整性校验）
# ======================================================================
@dataclass
class PackVerifyResult:
    """verify_pack 的结果。"""

    ok: bool
    unsigned: bool = False
    reasons: List[str] = field(default_factory=list)
    skills: List[str] = field(default_factory=list)


@dataclass
class PackSkillResult:
    """单个技能的安装结果明细。"""

    name: str
    status: str          # installed / conflict / blocked / error
    reason: str = ""


@dataclass
class PackInstallResult:
    """install_pack 的整体结果（区分成功 / 部分失败 / 整体失败）。"""

    ok: bool
    status: str
    results: List[PackSkillResult] = field(default_factory=list)
    conflict: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    unsigned: bool = False


class SkillPackManager:
    """技能包安装 / 更新管理（独立于 SkillManager，纯逻辑，无全局状态）。
    文件、hash 不匹配，任一情况都校验失败并返回具体原因列表；
    否则拒绝，防止任意路径写入；"""

    MANIFEST_NAME = "MANIFEST.sha256"

    def __init__(self, allowed_dirs: Optional[List[str]] = None):
        """allowed_dirs 可注入目标目录白名单（None = 从 SKILLS_CONFIG 读取）。"""
        self.allowed_dirs = allowed_dirs

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------
    def verify_pack(self, source_dir: str) -> PackVerifyResult:
        """校验技能包完整性，返回 PackVerifyResult。"""
        source_dir = os.path.abspath(source_dir)
        skills = self._list_skills(source_dir)
        manifest = os.path.join(source_dir, self.MANIFEST_NAME)

        if not os.path.isfile(manifest):
            # 未签名包：verify 通过但标注 unsigned，放行与否交给 install_pack
            return PackVerifyResult(ok=True, unsigned=True, skills=skills)

        reasons: List[str] = []
        expected: Dict[str, str] = {}
        try:
            with open(manifest, "r", encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        except OSError as exc:
            return PackVerifyResult(
                ok=False, unsigned=False, skills=skills,
                reasons=[f"无法读取 {self.MANIFEST_NAME}: {exc}"],
            )

        for lineno, line in enumerate(lines, 1):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue  # 空行 / 注释行跳过
            sha, rel = self._parse_manifest_line(stripped)
            if sha is None:
                reasons.append(
                    f"{self.MANIFEST_NAME} 第 {lineno} 行格式非法"
                    f"（应为 '<sha256>  <相对路径>'，两空格分隔）: {stripped!r}"
                )
                continue
            expected[rel] = sha

        # 磁盘实际文件（跳过符号链接；MANIFEST 自身不参与校验）
        disk_set = self._walk_files(source_dir)
        disk_set.discard(self.MANIFEST_NAME)

        # 1. manifest 未覆盖的文件
        for rel in sorted(disk_set):
            if rel not in expected:
                reasons.append(f"manifest 未覆盖文件: {rel}")

        # 2. 清单里多余但磁盘缺失的文件 / hash 不匹配
        for rel, sha in sorted(expected.items()):
            abs_f = os.path.join(source_dir, rel.replace("/", os.sep))
            if not os.path.isfile(abs_f) or os.path.islink(abs_f):
                reasons.append(f"清单中文件缺失: {rel}")
                continue
            try:
                with open(abs_f, "rb") as f:
                    actual = hashlib.sha256(f.read()).hexdigest()
            except OSError as exc:
                reasons.append(f"无法读取文件计算 hash: {rel}（{exc}）")
                continue
            if actual != sha:
                reasons.append(f"hash 不匹配: {rel}")

        return PackVerifyResult(
            ok=not reasons, unsigned=False, reasons=reasons, skills=skills,
        )

    # ------------------------------------------------------------------
    # 安装
    # ------------------------------------------------------------------
    def install_pack(self, source_dir: str, target_dir: str,
                     overwrite: bool = False,
                     allow_unsigned: bool = False) -> PackInstallResult:
        """安装 / 更新技能包到 target_dir。"""
        source_dir = os.path.abspath(source_dir)

        # 1. 目标目录必须是 SKILLS_CONFIG 配置的技能目录之一（防任意路径写入）
        if not self._is_allowed_target(target_dir):
            allowed = self._allowed_targets()
            return PackInstallResult(
                ok=False, status="failed",
                reasons=[
                    f"目标目录不是允许的技能目录: {target_dir}"
                    f"（允许: {', '.join(allowed) or '无'}）",
                ],
            )

        # 2. 完整性校验
        verify = self.verify_pack(source_dir)
        if not verify.ok:
            return PackInstallResult(
                ok=False, status="failed", unsigned=verify.unsigned,
                reasons=[f"技能包完整性校验失败（{len(verify.reasons)} 项）"]
                        + list(verify.reasons),
            )
        if verify.unsigned and not allow_unsigned:
            return PackInstallResult(
                ok=False, status="failed", unsigned=True,
                reasons=[
                    "技能包未签名（无 MANIFEST.sha256），默认拒绝安装；"
                    "确认安全后可使用 --skills-allow-unsigned 放行",
                ],
            )
        if not verify.skills:
            return PackInstallResult(
                ok=False, status="failed", unsigned=verify.unsigned,
                reasons=[f"技能包中没有任何技能（{source_dir} 下没有含 SKILL.md 的子目录）"],
            )

        # 3. 逐技能安装
        results: List[PackSkillResult] = []
        conflict: List[str] = []
        installed = 0
        for name in verify.skills:
            if not SkillManager._is_safe_name(name):
                results.append(PackSkillResult(
                    name=name, status="error", reason="技能名不安全（含 '..' 或路径分隔符）",
                ))
                continue
            dst = os.path.join(target_dir, name)
            if os.path.exists(dst) and not overwrite:
                conflict.append(name)
                results.append(PackSkillResult(
                    name=name, status="conflict", reason="目标已存在同名技能（可用 overwrite 覆盖）",
                ))
                continue
            try:
                copied, skipped, blocked = self._copy_skill(
                    os.path.join(source_dir, name), dst, overwrite=overwrite,
                )
            except OSError as exc:
                results.append(PackSkillResult(
                    name=name, status="error", reason=f"复制失败: {exc}",
                ))
                continue
            if blocked:
                results.append(PackSkillResult(
                    name=name, status="blocked",
                    reason=f"存在路径穿越条目（'..' 或绝对路径），已拒绝: {', '.join(blocked)}",
                ))
                continue
            detail = f"已安装 {copied} 个文件"
            if skipped:
                detail += f"，跳过 {len(skipped)} 个符号链接"
            results.append(PackSkillResult(name=name, status="installed", reason=detail))
            installed += 1

        if installed == len(results):
            status = "success"
        elif installed > 0:
            status = "partial"
        else:
            status = "failed"
        return PackInstallResult(
            ok=status == "success", status=status,
            results=results, conflict=conflict,
            unsigned=verify.unsigned,
        )

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    @classmethod
    def _list_skills(cls, source_dir: str) -> List[str]:
        """source_dir 下每个含 SKILL.md 的子目录名（排序）；目录不可读时返回空。"""
        if not os.path.isdir(source_dir):
            return []
        try:
            entries = sorted(os.listdir(source_dir))
        except OSError:
            return []
        skills = []
        for entry in entries:
            if not SkillManager._is_safe_name(entry):
                continue
            if os.path.isfile(os.path.join(source_dir, entry, "SKILL.md")):
                skills.append(entry)
        return skills

    @staticmethod
    def _parse_manifest_line(line: str) -> Tuple[Optional[str], Optional[str]]:
        """解析 '<sha256>  <相对路径>'（两空格分隔）。"""
        if len(line) < 64:
            return None, None
        sha = line[:64]
        rest = line[64:]
        if not rest.startswith("  "):
            return None, None
        rel = rest[2:].strip()
        if not rel:
            return None, None
        if not re.fullmatch(r"[0-9a-fA-F]{64}", sha):
            return None, None
        return sha.lower(), rel

    def _walk_files(self, source_dir: str) -> set:
        """递归收集 source_dir 下所有普通文件相对路径（posix 分隔），跳过符号链接。"""
        files: set = set()
        for root, _dirs, fnames in os.walk(source_dir, followlinks=False):
            for fname in fnames:
                abs_f = os.path.join(root, fname)
                if os.path.islink(abs_f):
                    continue
                files.add(os.path.relpath(abs_f, source_dir).replace(os.sep, "/"))
        return files

    @staticmethod
    def _unsafe_rel_path(rel: str) -> bool:
        """相对路径是否包含 '..' 分段或为绝对路径（防路径穿越）。"""
        if os.path.isabs(rel):
            return True
        parts = rel.replace("\\", "/").split("/")
        return ".." in parts

    def _copy_skill(self, src: str, dst: str, overwrite: bool):
        """复制单个技能目录 src → dst。"""
        # 收集条目（目录 + 文件），统一检查
        dirs_to_make: List[str] = []
        files_to_copy: List[Tuple[str, str]] = []
        skipped: List[str] = []
        blocked: List[str] = []

        for root, dirs, fnames in os.walk(src, followlinks=False):
            rel_root = os.path.relpath(root, src)
            for d in dirs:
                abs_d = os.path.join(root, d)
                if os.path.islink(abs_d):
                    skipped.append(os.path.relpath(abs_d, src).replace(os.sep, "/") + "/")
                    continue
                rel_d = os.path.relpath(abs_d, src).replace(os.sep, "/")
                if self._unsafe_rel_path(rel_d):
                    blocked.append(rel_d)
                else:
                    dirs_to_make.append(rel_d)
            for fname in fnames:
                abs_f = os.path.join(root, fname)
                rel_f = os.path.relpath(abs_f, src).replace(os.sep, "/")
                if os.path.islink(abs_f):
                    skipped.append(rel_f)
                    continue
                if self._unsafe_rel_path(rel_f):
                    blocked.append(rel_f)
                else:
                    files_to_copy.append((rel_f, abs_f))

        if blocked:
            return 0, skipped, blocked  # 有路径穿越条目：整体拒绝

        # 清理旧目录（overwrite 语义：先删后写，避免残留旧文件）
        if overwrite and os.path.isdir(dst):
            shutil.rmtree(dst)
        os.makedirs(dst, exist_ok=True)
        for rel_d in dirs_to_make:
            os.makedirs(os.path.join(dst, rel_d), exist_ok=True)
        for rel_f, abs_f in files_to_copy:
            target = os.path.join(dst, rel_f)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(abs_f, target)
        return len(files_to_copy), skipped, blocked

    # ------------------------------------------------------------------
    # 目标目录白名单（SKILLS_CONFIG 配置的技能目录）
    # ------------------------------------------------------------------
    def _allowed_targets(self) -> List[str]:
        if self.allowed_dirs is not None:
            dirs = self.allowed_dirs
        else:
            from config import SKILLS_CONFIG
            dirs = (SKILLS_CONFIG["project_dir"], SKILLS_CONFIG["user_dir"])
        normalized = []
        for d in dirs:
            if not d:
                continue
            expanded = os.path.abspath(os.path.expanduser(d))
            normalized.append(os.path.normcase(os.path.normpath(expanded)))
        return normalized

    def _is_allowed_target(self, target_dir: str) -> bool:
        target_norm = os.path.normcase(
            os.path.normpath(os.path.abspath(os.path.expanduser(target_dir)))
        )
        return target_norm in self._allowed_targets()

    def __repr__(self) -> str:  # pragma: no cover
        return (f"<SkillPackManager allowed_dirs={self.allowed_dirs!r}>")