"""
Skills 技能包机制（技能包式能力扩展，最小可用实现）。

技能 = 一个包含 SKILL.md 的子目录。SKILL.md 头部可选 frontmatter：
    ---
    name: excel
    description: 生成 Excel 报表
    triggers: excel, 报表, 表格
    ---
    正文……

frontmatter 使用极简自写解析器（禁止 yaml 依赖）：
- 缺失 / 未闭合 / 解析失败时降级：name 用目录名、description 置空，正文从
  第一个非 frontmatter 行开始；
- frontmatter 内损坏行（无冒号）静默跳过，不影响其余字段解析。

用法：
    from agent.skills import SkillManager

    mgr = SkillManager(project_dir="./skills",
                       user_dir="~/.my_agent/skills",
                       max_chars=6000)
    skills = mgr.discover()                 # 按技能名排序的技能列表
    hits = mgr.match("帮我生成 excel 报表")   # 关键词命中
    text = mgr.render_for_prompt(goal)      # 注入系统提示的渲染文本

安全（重要，v2 脚本发现）：脚本发现只做「告知」——扫描技能目录第一层普通
文件并登记相对路径，渲染时列出绝对路径，**绝不自动放行 / 自动执行**。任何
脚本执行仍走终端工具 → 审批门（execpolicy 白名单 / 黑名单）原样生效；用户
希望特定脚本免询问执行，需在 execpolicy.json 显式添加规则。Skills 层绝不绕过
审批。其余：仅读取目录文件，无任何写操作；技能名（目录名）含路径分隔符
或 ".." 时跳过。
"""

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
    """一个已加载的技能。

    scripts: 相对技能目录的脚本路径列表（仅第一层普通文件、按文件名排序；
              SKILL.md 与隐藏文件不计入）。仅用于「告知」渲染，绝不自动放行执行。
    """

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
        """扫描项目级与用户级技能目录，返回按技能名排序的技能列表（稳定）。

        - 每个含 SKILL.md 的子目录即一个技能；
        - 目录不存在 / 不可读时静默跳过；
        - 同名技能（跨目录）去重，项目级优先于用户级；
        - 技能名含路径分隔符或 ".." 时跳过；
        - 每个技能目录还会扫描第一层普通文件登记进 scripts（仅告知，不执行）。
        """
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
        """扫描技能目录第一层的普通文件，登记相对路径（按文件名排序）。

        规则：
        - 仅第一层，不递归子目录；
        - 跳过隐藏文件（点开头）；
        - SKILL.md 自身不登记；
        - 目录不可读等异常静默降级为空列表。

        安全边界（重要）：此处只做「告知」——扫描结果仅用于渲染提示（绝对路径
        清单 / 计数），**绝不自动放行或执行**。任何脚本执行仍走终端工具 →
        审批门（execpolicy 白名单 / 黑名单）原样生效；用户希望特定脚本免询问
        执行，需在 execpolicy.json 显式添加规则。Skills 层绝不绕过审批。
        """
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
        """极简 frontmatter 解析（支持 YAML folded/literal 块与多行缩进续行）。

        返回 (meta_dict, body_start)：
        - 无 frontmatter（首行不是 '---'）：({}, 0)，正文从第 1 行开始；
        - 有闭合 frontmatter：解析内部 key: value 行（损坏行跳过），
          description/name 支持多行缩进续行与 '>'/'|' 块指示符；
          正文从闭合行之后开始；
        - 首行是 '---' 但未闭合：解析失败，降级为 ({}, 0)。
        """
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
        """对 goal 做关键词命中判断（name / description / triggers）。

        大小写不敏感：triggers 任一关键词包含命中，或 name / description
        分词（整段也算一个词）包含命中。
        """
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
            if len(word) >= 2 and word in goal_lower:
                return True
        return False

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------
    def render_for_prompt(self, goal: str) -> str:
        """渲染注入文本：全部技能一句话索引 + 命中技能的完整正文。

        - 无技能（目录不存在 / 目录内无技能）时返回空串；
        - 技能索引行对含脚本的技能标注「（含 N 个脚本）」计数提示；
        - 命中技能正文之后追加「附带脚本」清单（每行一条绝对路径，仅告知），
          无脚本的技能不加这段；
        - 总长超过 max_chars 时截断并注明（skill 截断）。

        安全：脚本清单只做「告知」，不自动放行执行（见模块 docstring）。
        """
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
        parts.append("技能索引：\n" + "\n".join(index_lines))
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
    """verify_pack 的结果。

    - ok:      校验是否通过（signed 包全部文件 hash 匹配、无多余/缺失条目）
    - unsigned: 无 MANIFEST.sha256 的未签名包（verify 本身不判失败，
                是否放行由 install_pack 的 allow_unsigned 决定）
    - reasons: 具体失败原因列表（通过时为空）
    - skills:  包内技能名列表（含 SKILL.md 的子目录名，排序）
    """

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
    """install_pack 的整体结果（区分成功 / 部分失败 / 整体失败）。

    - ok:      是否整体成功（全部技能安装完成）
    - status:  success（全成功）/ partial（部分失败）/ failed（整体失败）
    - results: 每个技能的结果明细
    - conflict: 因同名冲突被拒绝的技能名列表
    - reasons:  整体失败原因（目标目录不允许 / 未签名被拒 / 校验失败等）
    - unsigned: 该包是否未签名
    """

    ok: bool
    status: str
    results: List[PackSkillResult] = field(default_factory=list)
    conflict: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    unsigned: bool = False


class SkillPackManager:
    """技能包安装 / 更新管理（独立于 SkillManager，纯逻辑，无全局状态）。

    一个「技能包」= source_dir 目录，其中每个含 SKILL.md 的子目录是一个技能。

    完整性校验（verify_pack）：
    - 若存在 MANIFEST.sha256（行格式 '<sha256>  <相对路径>'，两空格分隔），
      逐文件校验 SHA-256；manifest 未覆盖的文件、清单里多余但磁盘缺失的
      文件、hash 不匹配，任一情况都校验失败并返回具体原因列表；
    - 无 manifest 的包视为未签名（unsigned），verify 结果标注 unsigned，
      安装时由 allow_unsigned 参数决定是否放行（默认 False 拒绝）。

    安全边界：
    - target_dir 必须是 SKILLS_CONFIG 配置的技能目录之一（project / user），
      否则拒绝，防止任意路径写入；
    - 复制时跳过符号链接，拒绝相对路径中包含 '..' 或绝对路径的条目（防路径穿越）；
    - 同名技能冲突默认拒绝，overwrite=True 时才覆盖更新。

    用法：
        from agent.skills import SkillPackManager
        mgr = SkillPackManager()   # 目标目录白名单取自 SKILLS_CONFIG
        result = mgr.install_pack("./my-pack", "./skills",
                                  overwrite=False, allow_unsigned=False)
    """

    MANIFEST_NAME = "MANIFEST.sha256"

    def __init__(self, allowed_dirs: Optional[List[str]] = None):
        """allowed_dirs 可注入目标目录白名单（None = 从 SKILLS_CONFIG 读取）。"""
        self.allowed_dirs = allowed_dirs

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------
    def verify_pack(self, source_dir: str) -> PackVerifyResult:
        """校验技能包完整性，返回 PackVerifyResult。

        - 无 MANIFEST.sha256 → ok=True, unsigned=True（未签名，不判失败）；
        - 有 manifest → 逐文件 SHA-256 校验，任何不一致给出具体原因列表。
        """
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
        """安装 / 更新技能包到 target_dir。

        流程：先 verify → 校验通过且（signed 或 allow_unsigned）→ 逐技能复制。
        目标已存在同名技能且 overwrite=False → 该技能拒绝（冲突清单）。
        """
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
        """解析 '<sha256>  <相对路径>'（两空格分隔）。

        返回 (sha, rel)；格式非法返回 (None, None)。
        """
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
        """复制单个技能目录 src → dst。

        - 先收集并检查所有条目（跳过符号链接；拒绝 '..'/绝对路径）；
        - 存在被拒条目时整体拒绝复制（不半途而废）；
        - 返回 (copied_files, skipped_links, blocked_entries)。
        """
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