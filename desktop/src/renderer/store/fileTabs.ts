/**
 * 文件标签页状态：文件树点击 → 中央列以 Tab 方式打开。
 * 内容只读（200KB 上限由后端截断）；会话级生命周期（不持久化）。
 */
import { create } from 'zustand';
import { fetchFileContent } from '../lib/backend';

export interface OpenFileTab {
  path: string;
  content: string;
  truncated: boolean;
  error?: string;
}

interface FileTabsState {
  files: OpenFileTab[];
  activePath: string | null;            // null = 对话 Tab
  openFile: (path: string) => Promise<void>;
  closeFile: (path: string) => void;
  setActive: (path: string | null) => void;
}

export const useFileTabsStore = create<FileTabsState>((set, get) => ({
  files: [],
  activePath: null,
  openFile: async (path) => {
    set({ activePath: path });
    if (get().files.some((f) => f.path === path)) return;
    const r = await fetchFileContent(path);
    const tab: OpenFileTab = r.ok
      ? { path, content: r.content || '', truncated: Boolean(r.truncated) }
      : { path, content: '', truncated: false, error: r.error || '读取失败' };
    set((s) => ({
      files: s.files.some((f) => f.path === path)
        ? s.files.map((f) => (f.path === path ? { ...f, ...tab } : f))
        : [...s.files, tab],
    }));
  },
  closeFile: (path) =>
    set((s) => {
      const files = s.files.filter((f) => f.path !== path);
      return { files, activePath: s.activePath === path ? (files[0]?.path ?? null) : s.activePath };
    }),
  setActive: (path) => set({ activePath: path }),
}));
