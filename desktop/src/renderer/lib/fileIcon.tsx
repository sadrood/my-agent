/**
 * 文件类型图标与配色（高信息密度文件树）。
 * 按扩展名映射到 lucide 图标 + 主题色；未知类型回退通用文件图标。
 */
import React from 'react';
import {
  FileText, FileCode, FileJson, FileType, FileImage,
  FileArchive, FileSpreadsheet, File, Braces, Database,
  TerminalSquare, Palette, FileMusic, FileVideo, Folder,
} from 'lucide-react';

interface IconSpec {
  Icon: typeof FileText;
  color: string;
}

const ICONS: Record<string, IconSpec> = {
  // 代码
  py: { Icon: FileCode, color: '#3572A5' },
  js: { Icon: FileCode, color: '#F1E05A' },
  ts: { Icon: Braces, color: '#3178C6' },
  tsx: { Icon: Braces, color: '#3178C6' },
  jsx: { Icon: Braces, color: '#61DAFB' },
  json: { Icon: FileJson, color: '#CBCB41' },
  html: { Icon: FileCode, color: '#E34F26' },
  css: { Icon: Palette, color: '#663399' },
  md: { Icon: FileText, color: '#6E7681' },
  // 数据/配置
  yml: { Icon: Database, color: '#8B8B8B' },
  yaml: { Icon: Database, color: '#8B8B8B' },
  toml: { Icon: Database, color: '#8B8B8B' },
  ini: { Icon: Database, color: '#8B8B8B' },
  bat: { Icon: TerminalSquare, color: '#C1C1C1' },
  cmd: { Icon: TerminalSquare, color: '#C1C1C1' },
  sh: { Icon: TerminalSquare, color: '#89E051' },
  // 文档
  txt: { Icon: FileType, color: '#9DA5B0' },
  docx: { Icon: FileType, color: '#2B579A' },
  pdf: { Icon: FileType, color: '#E02626' },
  // 表格
  xlsx: { Icon: FileSpreadsheet, color: '#107C41' },
  csv: { Icon: FileSpreadsheet, color: '#107C41' },
  // 图片/媒体
  png: { Icon: FileImage, color: '#9669ED' },
  jpg: { Icon: FileImage, color: '#9669ED' },
  jpeg: { Icon: FileImage, color: '#9669ED' },
  gif: { Icon: FileImage, color: '#9669ED' },
  svg: { Icon: FileImage, color: '#FFB13B' },
  mp3: { Icon: FileMusic, color: '#1ED760' },
  wav: { Icon: FileMusic, color: '#1ED760' },
  mp4: { Icon: FileVideo, color: '#C1C1C1' },
  // 压缩包
  zip: { Icon: FileArchive, color: '#C1C1C1' },
  tar: { Icon: FileArchive, color: '#C1C1C1' },
  gz: { Icon: FileArchive, color: '#C1C1C1' },
};

/** 通用文件（无扩展名或未知类型） */
const FALLBACK: IconSpec = { Icon: File, color: '#9DA5B0' };

/** 按文件路径取图标规格。 */
export function fileIconFor(path: string): IconSpec {
  const base = path.split(/[\\/]/).pop() || '';
  const dot = base.lastIndexOf('.');
  if (dot <= 0) return FALLBACK;
  const ext = base.slice(dot + 1).toLowerCase();
  return ICONS[ext] || FALLBACK;
}

/** git 状态 → 颜色（新增绿 / 修改橙 / 删除红）。 */
export function gitColorFor(state: string): string {
  if (state === 'A' || state === '?') return '#4CAF50';
  if (state === 'D') return '#F44336';
  return '#E0A030'; // M 修改
}

/** 渲染文件类型图标（供树节点/搜索行复用）。 */
export function FileTypeIcon({ path, size = 11 }: { path: string; size?: number }) {
  const { Icon, color } = fileIconFor(path);
  return <Icon size={size} style={{ color, flexShrink: 0 }} />;
}