/** 主题解析：用户选择 light/dark/system，渲染层只需要最终 light/dark。 */
export type ThemeChoice = 'light' | 'dark' | 'system';

export function resolveTheme(choice?: string | null): 'light' | 'dark' {
  if (choice === 'light' || choice === 'dark') return choice;
  try {
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  } catch {
    return 'light';
  }
}
