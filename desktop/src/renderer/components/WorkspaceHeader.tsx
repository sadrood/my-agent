/**
 * 工作区头：项目名 + Git 分支徽章 + 文件/文件夹统计 + 路径。
 * 数据源：/api/git（分支）+ /api/files（树统计）——均为已有只读接口。
 * 文件树变更事件（工具写入）时自动刷新统计。
 */
import React, { useEffect, useMemo, useState } from 'react';
import { Zap, GitBranch, FileText } from 'lucide-react';
import { fetchGit, fetchFileTree, type FileTreeNode } from '../lib/backend';
import { useProjectStore } from '../store/project';
import { useBackendStore } from '../store';

function countTree(node: FileTreeNode): { files: number; dirs: number } {
  let files = 0;
  let dirs = 0;
  const walk = (n: FileTreeNode) => {
    if (n.type === 'dir') {
      dirs++;
      for (const c of n.children || []) walk(c);
    } else {
      files++;
    }
  };
  walk(node);
  return { files, dirs };
}

/** 影响文件系统的工具：其成功结果触发工作区头刷新（对齐 FileTree 的 FS_TOOLS） */
const FS_TOOLS = new Set(['file', 'edit', 'patch', 'python', 'terminal', 'image_gen']);

export function WorkspaceHeader() {
  const activeProject = useProjectStore((s) => s.getActive());
  const events = useBackendStore((s) => s.events);
  const [branch, setBranch] = useState('');
  const [stats, setStats] = useState({ files: 0, dirs: 0 });

  // 只在「文件写操作成功」事件后刷新，避免事件流里每条 token/状态事件都触发拉取
  const writeTick = useMemo(() => {
    let tick = 0;
    for (const e of events) {
      if (e.type === 'tool_result' && Boolean(e.data?.success) && FS_TOOLS.has(String(e.data?.tool || ''))) tick++;
    }
    return tick;
  }, [events]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      const [git, tree] = await Promise.all([fetchGit(), fetchFileTree()]);
      if (cancelled) return;
      if (git?.in_repo) setBranch(git.branch || '');
      if (tree?.tree) setStats(countTree(tree.tree));
    })();
    return () => { cancelled = true; };
  }, [writeTick]);

  const name = activeProject?.name || '工作区';
  const statText = `${stats.files} 文件 · ${stats.dirs} 文件夹`;
  return (
    <div className="ws-head">
      <div className="ws-title" title={activeProject?.path || ''}>
        <Zap size={12} />
        <span className="ws-name">{name}</span>
        {branch && <span className="ws-branch"><GitBranch size={10} /> {branch}</span>}
      </div>
      <div className="ws-stat" title={activeProject?.path || ''}>
        <FileText size={10} /> {statText}
      </div>
    </div>
  );
}
