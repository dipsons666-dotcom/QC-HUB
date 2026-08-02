export const DASHBOARD_SIDEBAR_COLOR_KEY = "dashboard.sidebar.hsl";
export const DASHBOARD_SIDEBAR_HOVER_HSL = "188 84% 53%";

export function setDashboardSidebarHoverColor() {
  try {
    localStorage.setItem(DASHBOARD_SIDEBAR_COLOR_KEY, DASHBOARD_SIDEBAR_HOVER_HSL);
  } catch {
    // Ignore storage errors.
  }
}

export function getDashboardSidebarColor(): string | null {
  try {
    return localStorage.getItem(DASHBOARD_SIDEBAR_COLOR_KEY);
  } catch {
    return null;
  }
}
