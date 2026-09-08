/**
 * 项目（Project）store：项目 = 一个工作目录 + 其下的会话集合。
 * - 项目持久化到 localStorage（my-agent-projects）。
 * - 当前项目 path 即后端 workdir（切换项目 = backendRestart(project.path)）。
 */
import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import type { Project } from '../lib/types';

interface ProjectState {
  projects: Project[];
  activeProjectId: string | null;
  /** 添加项目（按路径去重；重复则切换激活）。返回项目或 null */
  addProject: (path: string) => Project | null;
  removeProject: (id: string) => void;
  setActive: (id: string) => void;
  getActive: () => Project | null;
}

/** 目录路径 → 项目名（取最后一段） */
function nameFromPath(path: string): string {
  return path.split(/[\\/]/).filter(Boolean).pop() || path;
}

export const useProjectStore = create<ProjectState>()(
  persist(
    (set, get) => ({
      projects: [],
      activeProjectId: null,
      addProject: (path) => {
        const p = (path || '').trim();
        if (!p) return null;
        const existing = get().projects.find((x) => x.path === p);
        if (existing) {
          set({ activeProjectId: existing.id });
          return existing;
        }
        const project: Project = {
          id: `proj-${Date.now().toString(36)}`,
          name: nameFromPath(p),
          path: p,
        };
        set((s) => ({ projects: [...s.projects, project], activeProjectId: project.id }));
        return project;
      },
      removeProject: (id) =>
        set((s) => {
          const rest = s.projects.filter((x) => x.id !== id);
          const nextActive = s.activeProjectId === id
            ? (rest[0]?.id ?? null)
            : s.activeProjectId;
          return { projects: rest, activeProjectId: nextActive };
        }),
      setActive: (id) => set({ activeProjectId: id }),
      getActive: () => get().projects.find((x) => x.id === get().activeProjectId) ?? null,
    }),
    { name: 'my-agent-projects' },
  ),
);
