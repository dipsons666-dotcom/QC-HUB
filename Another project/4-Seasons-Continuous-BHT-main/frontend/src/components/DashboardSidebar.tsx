import {
  Activity,
  ChevronDown,
  Download,
  Eye,
  Flame,
  LayoutDashboard,
  LogOut,
  Megaphone,
  Menu,
  Package,
  RadioTower,
  Search,
  ShoppingCart,
  Sparkles,
  Table2,
  Target,
  ClipboardCheck,
  Users,
} from "lucide-react";
import { NavLink } from "@/components/NavLink";
import { useAuth } from "@/contexts/AuthContext";
import { useLocation, useNavigate } from "react-router-dom";
import { useEffect, useState } from "react";
import type { ComponentType } from "react";
import { getAdminAccessibleCategories, isMaltCategorySlug } from "@/data/categories";
import { hasVerbatimQuestionOptions, VERBATIMS_TOPICS_ROUTE_SEGMENT } from "@/lib/verbatimTopics";
import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarMenuSub,
  SidebarMenuSubButton,
  SidebarMenuSubItem,
  SidebarFooter,
  SidebarHeader,
  useSidebar,
} from "@/components/ui/sidebar";

interface Props {
  slug: string;
  pages?: Array<{ id: string; title: string }>;
}

const AWARENESS_SUBPAGES = [
  { id: "awareness-usage__awareness", title: "Awareness" },
  { id: "awareness-usage__media-source", title: "Media Source" },
  { id: "awareness-usage__usage", title: "Usage" },
];
const OVERVIEW_SUBPAGES = [
  { id: "overview__demographics", title: "Demographics" },
  { id: "overview__quota", title: "Quota", url: "https://inicio-quota-tracking.netlify.app/" },
];
const FLAVOUR_FLEX_SUBPAGES = [
  { id: "flavour-flex-section__flavour", title: "Flavour" },
  { id: "flavour-flex-section__flex", title: "Flex" },
];
const CAMPAIGN_CHECK_SUBPAGE_PREFIX = "campaign-check__";
const CAMPAIGN_CHECK_SUBPAGES = [
  { id: "campaign-check__radio-jingle", title: "Radio Jingle" },
  { id: "campaign-check__flyer", title: "Flyer" },
];
const CAMPAIGN_CHECK_ALLOWED_SLUGS = new Set(["noodles", "toothpaste", "snacks", "bleach", "toilet-cleaner"]);
const SNACKS_CAMPAIGN_CHECK_SLUG = "snacks";
const FLAVOUR_FLEX_ALLOWED_SLUGS = new Set(["noodles"]);

const ICON_BY_PAGE_ID: Record<string, ComponentType<{ className?: string }>> = {
  overview: LayoutDashboard,
  "overview__demographics": Users,
  "overview__quota": LayoutDashboard,
  "awareness-usage": Eye,
  "awareness-usage__awareness": Eye,
  "awareness-usage__media-source": RadioTower,
  "awareness-usage__usage": Activity,
  "purchase-behavior": ShoppingCart,
  "brand-imagery": Sparkles,
  "flavour-flex-section": Flame,
  "flavour-flex-section__flavour": Flame,
  "flavour-flex-section__flex": Activity,
  "campaign-check": Megaphone,
  "campaign-check__radio-jingle": RadioTower,
  "campaign-check__flyer": Target,
  "video-ad-section": Target,
  "screener-demographics": Search,
  "category-questions": Package,
  "other-metrics": Flame,
  "custom-table": Table2,
  "export-report": Download,
  "verbatims-topics": Search,
};

function iconForPage(pageId: string, title: string) {
  if (ICON_BY_PAGE_ID[pageId]) return ICON_BY_PAGE_ID[pageId];
  const normalized = String(title || "").toLowerCase();
  if (normalized.includes("awareness")) return Eye;
  if (normalized.includes("media")) return RadioTower;
  if (normalized.includes("usage")) return Activity;
  if (normalized.includes("purchase")) return ShoppingCart;
  if (normalized.includes("imagery")) return Sparkles;
  if (normalized.includes("campaign")) return Megaphone;
  return LayoutDashboard;
}

export function DashboardSidebar({ slug, pages = [] }: Props) {
  const { state, isMobile, openMobile, setOpenMobile, setOpen } = useSidebar();
  const collapsed = !isMobile && state === "collapsed";
  const { user, logout, selectedAdminCategory, setSelectedAdminCategoryBySlug } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const isAdmin = user?.role === "admin";
  const dashboardBasePath = isAdmin ? "/admin/dashboard" : "/dashboard";
  const categoryLabel =
    (user && user.role === "category" ? user.category.category : selectedAdminCategory?.category) ||
    slug.replace(/-/g, " ").replace(/\b\w/g, (ch) => ch.toUpperCase());
  const adminCategories = getAdminAccessibleCategories();
  const isMalt = isMaltCategorySlug(slug);
  const activeItemClass = isMalt
    ? "bg-[#C8A44D] text-slate-950 [&>svg]:text-slate-950 shadow-[0_10px_20px_rgba(200,164,77,0.38)]"
    : "bg-red-500 text-white [&>svg]:text-white shadow-[0_10px_20px_rgba(239,68,68,0.45)]";
  const categoryFocusBorderClass = isMalt ? "focus:border-[#C8A44D]" : "focus:border-red-400";

  const overviewItem = {
    id: "overview",
    title: "Overview",
    url: `${dashboardBasePath}/${slug}`,
    icon: LayoutDashboard,
    isSub: false,
    isDisabled: false,
  };
  const customTableItem = {
    id: "custom-table",
    title: "Custom Table",
    url: `${dashboardBasePath}/${slug}/custom-table`,
    icon: Table2,
    isSub: false,
    isDisabled: false,
  };
  const exportReportItem = {
    id: "export-report",
    title: "Data Table Exports",
    url: `${dashboardBasePath}/${slug}/export-report`,
    icon: Download,
    isSub: false,
    isDisabled: false,
  };
  const metadataReviewItem = {
    id: "metadata-review",
    title: "Metadata Review",
    url: "/admin/metadata-review",
    icon: ClipboardCheck,
    isSub: false,
    isDisabled: false,
  };
  const verbatimsTopicsItem = {
    id: "verbatims-topics",
    title: "Verbatims & Topics",
    url: `${dashboardBasePath}/${slug}/${VERBATIMS_TOPICS_ROUTE_SEGMENT}`,
    icon: Search,
    isSub: false,
    isDisabled: false,
  };

  const questionItems = pages.map((page) => ({
    id: page.id,
    title: page.title,
    url: `${dashboardBasePath}/${slug}/page/${page.id}`,
    icon: iconForPage(page.id, page.title),
    isSub: false,
    isDisabled: false,
  }));
  const purchaseUrl = `${dashboardBasePath}/${slug}/page/purchase-behavior`;
  const brandImageryUrl = `${dashboardBasePath}/${slug}/page/brand-imagery`;
  const flavourFlexUrl = `${dashboardBasePath}/${slug}/page/flavour-flex-section`;
  const campaignCheckUrl = `${dashboardBasePath}/${slug}/page/campaign-check`;
  const canShowCampaignCheck = CAMPAIGN_CHECK_ALLOWED_SLUGS.has(slug);
  const hasCampaignCheckSubpages = canShowCampaignCheck && slug === SNACKS_CAMPAIGN_CHECK_SLUG;
  const canShowFlavourFlex = FLAVOUR_FLEX_ALLOWED_SLUGS.has(slug);
  const canShowVerbatimTopics = hasVerbatimQuestionOptions(slug);

  const awarenessItem = questionItems.find((item) => item.title.toLowerCase() === "awareness & usage");
  const isOverviewActive = location.pathname === `${dashboardBasePath}/${slug}`;
  const isAwarenessSubpageActive = location.pathname.includes(`${dashboardBasePath}/${slug}/page/awareness-usage__`);
  const flavourFlexItem = canShowFlavourFlex ? questionItems.find((item) => item.url === flavourFlexUrl) : undefined;
  const isFlavourFlexSubpageActive =
    canShowFlavourFlex && location.pathname.includes(`${dashboardBasePath}/${slug}/page/flavour-flex-section__`);
  const isCampaignCheckSubpageActive =
    hasCampaignCheckSubpages && location.pathname.includes(`${dashboardBasePath}/${slug}/page/${CAMPAIGN_CHECK_SUBPAGE_PREFIX}`);
  let purchaseBehaviourItem = questionItems.find((item) => item.url === purchaseUrl);
  if (!purchaseBehaviourItem) {
    purchaseBehaviourItem = {
      title: "Purchase Behaviour",
      url: purchaseUrl,
      icon: ShoppingCart,
      isSub: false,
      isDisabled: false,
    };
  }
  let flavourFlexSectionItem = flavourFlexItem;
  if (canShowFlavourFlex && !flavourFlexSectionItem) {
    flavourFlexSectionItem = {
      id: "flavour-flex-section",
      title: "Flavour/Flex Section",
      url: flavourFlexUrl,
      icon: Flame,
      isSub: false,
      isDisabled: false,
    };
  }
  let campaignCheckItem = questionItems.find((item) => item.url === campaignCheckUrl);
  if (canShowCampaignCheck && !campaignCheckItem) {
    campaignCheckItem = {
      title: "Campaign Check",
      url: campaignCheckUrl,
      icon: Megaphone,
      isSub: false,
      isDisabled: false,
    };
  }
  let brandImageryItem = questionItems.find((item) => item.url === brandImageryUrl);
  if (!brandImageryItem) {
    brandImageryItem = {
      title: "Brand Imagery",
      url: brandImageryUrl,
      icon: Sparkles,
      isSub: false,
      isDisabled: false,
    };
  }
  const otherQuestionItems = questionItems.filter(
    (item) =>
      item !== awarenessItem &&
      item.url !== purchaseUrl &&
      item.url !== brandImageryUrl &&
      item.url !== flavourFlexUrl &&
      item.url !== campaignCheckUrl,
  );
  const awarenessSubItems = awarenessItem
    ? AWARENESS_SUBPAGES.map((sub) => ({
        id: sub.id,
        title: sub.title,
        url: `${dashboardBasePath}/${slug}/page/${sub.id}`,
        icon: iconForPage(sub.id, sub.title),
        isSub: true,
      }))
    : [];
  const flavourFlexSubItems = canShowFlavourFlex && flavourFlexSectionItem
    ? FLAVOUR_FLEX_SUBPAGES.map((sub) => ({
        id: sub.id,
        title: sub.title,
        url: `${dashboardBasePath}/${slug}/page/${sub.id}`,
        icon: iconForPage(sub.id, sub.title),
        isSub: true,
      }))
    : [];
  const campaignCheckSubItems = hasCampaignCheckSubpages && campaignCheckItem
    ? CAMPAIGN_CHECK_SUBPAGES.map((sub) => ({
        id: sub.id,
        title: sub.title,
        url: `${dashboardBasePath}/${slug}/page/${sub.id}`,
        icon: iconForPage(sub.id, sub.title),
        isSub: true,
      }))
    : [];
  const overviewSubItems = OVERVIEW_SUBPAGES.map((sub) => ({
    id: sub.id,
    title: sub.title,
    url: sub.url || `${dashboardBasePath}/${slug}`,
    icon: iconForPage(sub.id, sub.title),
    isSub: true,
    external: Boolean(sub.url),
  }));
  const activeSubmenuKey = isOverviewActive
    ? "overview"
    : isAwarenessSubpageActive
      ? "awareness-usage"
    : isFlavourFlexSubpageActive
      ? "flavour-flex-section"
      : isCampaignCheckSubpageActive
        ? "campaign-check"
      : null;
  const [expandedSubmenuKey, setExpandedSubmenuKey] = useState<string | null>(activeSubmenuKey);
  const itemsBetweenGroupedSections = [purchaseBehaviourItem, brandImageryItem];
  const itemsAfterGroupedSections = [
    ...(!hasCampaignCheckSubpages && canShowCampaignCheck && campaignCheckItem ? [campaignCheckItem] : []),
    ...otherQuestionItems,
    ...(canShowVerbatimTopics ? [verbatimsTopicsItem] : []),
    customTableItem,
    exportReportItem,
    ...(isAdmin ? [metadataReviewItem] : []),
  ];

  useEffect(() => {
    if (activeSubmenuKey) {
      setExpandedSubmenuKey(activeSubmenuKey);
    }
  }, [activeSubmenuKey]);

  const handleLogout = () => {
    logout();
    navigate("/");
  };
  const handleAdminCategorySwitch = (nextSlug: string) => {
    if (!nextSlug || nextSlug === slug) return;
    setSelectedAdminCategoryBySlug(nextSlug);
    navigate(`${dashboardBasePath}/${nextSlug}`);
  };
  const handleGroupedSectionClick = (
    groupKey: "overview" | "awareness-usage" | "flavour-flex-section" | "campaign-check",
    defaultUrl: string,
    isGroupActive: boolean,
  ) => {
    setExpandedSubmenuKey((prev) => (prev === groupKey ? null : groupKey));
    if (!isGroupActive) {
      navigate(defaultUrl);
      if (isMobile) setOpenMobile(false);
    }
  };

  const handleExpandIfCollapsed = () => {
    if (!isMobile && collapsed) setOpen(true);
  };
  const handleCollapseOnLeave = () => {
    if (!isMobile && !collapsed) setOpen(false);
  };

  useEffect(() => {
    if (isMobile || collapsed) return;

    const handleClickOutside = (event: MouseEvent) => {
      const target = event.target as HTMLElement | null;
      if (!target) return;
      if (target.closest('[data-sidebar="sidebar"]')) return;
      setOpen(false);
    };

    document.addEventListener("mousedown", handleClickOutside);
    return () => document.removeEventListener("mousedown", handleClickOutside);
  }, [isMobile, collapsed, setOpen]);

  const renderNavItem = (item: { title: string; url: string; icon: ComponentType<{ className?: string }>; isSub?: boolean }) => (
    <SidebarMenuItem key={item.title}>
      <SidebarMenuButton asChild tooltip={item.title}>
        <NavLink
          to={item.url}
          end
          className={`rounded-xl text-sidebar-foreground/70 transition-all duration-200 hover:bg-blue-500/20 hover:text-sidebar-foreground ${
            collapsed
              ? "flex justify-center px-0 py-[18px] my-0.5"
              : item.isSub
                ? "flex items-center gap-3 pl-10 pr-4 py-4"
                : "flex items-center gap-3 px-4 py-[18px]"
          }`}
          activeClassName={`${activeItemClass} font-medium`}
        >
          <item.icon className="h-5 w-5 shrink-0" />
          {!collapsed && <span className={`${item.isSub ? "text-[14px]" : "text-[15px]"} font-medium`}>{item.title}</span>}
        </NavLink>
      </SidebarMenuButton>
    </SidebarMenuItem>
  );

  const renderGroupedSection = ({
    groupKey,
    item,
    subItems,
    isActive,
  }: {
    groupKey: "overview" | "awareness-usage" | "flavour-flex-section" | "campaign-check";
    item: { id?: string; title: string; url: string; icon: ComponentType<{ className?: string }> };
    subItems: Array<{ id?: string; title: string; url: string; icon: ComponentType<{ className?: string }>; isSub?: boolean; external?: boolean }>;
    isActive: boolean;
  }) => {
    const isExpanded = !collapsed && expandedSubmenuKey === groupKey;
    return (
      <SidebarMenuItem key={item.title}>
        <SidebarMenuButton
          tooltip={item.title}
          onClick={() => handleGroupedSectionClick(groupKey, subItems[0]?.url || item.url, isActive)}
          className={`rounded-xl text-sidebar-foreground/70 transition-all duration-200 hover:bg-blue-500/20 hover:text-sidebar-foreground ${
            collapsed ? "flex justify-center px-0 py-[18px] my-0.5" : "flex items-center gap-3 px-4 py-[18px]"
          } ${(isActive || isExpanded) ? activeItemClass : ""}`}
        >
          <item.icon className="h-5 w-5 shrink-0" />
          {!collapsed && (
            <>
              <span className="text-[15px] font-medium">{item.title}</span>
              <ChevronDown className={`ml-auto h-4 w-4 shrink-0 transition-transform ${isExpanded ? "rotate-180" : ""}`} />
            </>
          )}
        </SidebarMenuButton>
        {!collapsed && isExpanded ? (
          <SidebarMenuSub className="mt-1 gap-2 py-1.5">
            {subItems.map((subItem) => (
              <SidebarMenuSubItem key={subItem.title}>
                <SidebarMenuSubButton asChild className="h-9 rounded-xl px-3">
                  {subItem.external ? (
                    <a
                      href={subItem.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="rounded-lg text-sidebar-foreground/75 transition-all duration-200 hover:bg-blue-500/20 hover:text-sidebar-foreground"
                    >
                      <subItem.icon className="h-4 w-4 shrink-0" />
                      <span className="text-[14px] font-medium">{subItem.title}</span>
                    </a>
                  ) : (
                    <NavLink
                      to={subItem.url}
                      end
                      activeClassName={`${activeItemClass} font-medium`}
                      className="rounded-lg text-sidebar-foreground/75 transition-all duration-200 hover:bg-blue-500/20 hover:text-sidebar-foreground"
                    >
                      <subItem.icon className="h-4 w-4 shrink-0" />
                      <span className="text-[14px] font-medium">{subItem.title}</span>
                    </NavLink>
                  )}
                </SidebarMenuSubButton>
              </SidebarMenuSubItem>
            ))}
          </SidebarMenuSub>
        ) : null}
      </SidebarMenuItem>
    );
  };

  return (
    <>
      <Sidebar
        collapsible="icon"
        className="border-r-0 dashboard-sidebar-shell"
        onMouseEnter={handleExpandIfCollapsed}
        onMouseLeave={handleCollapseOnLeave}
        onClickCapture={handleExpandIfCollapsed}
      >
        <SidebarHeader className="h-20 border-b border-sidebar-border/30 p-4">
          <div className="flex items-center justify-center gap-3 pt-1">
            <img
              src="/infinity-logo.png"
              alt="Infinity Logo"
              className={`${collapsed ? "h-12 w-12" : "h-10 w-10"} shrink-0 object-contain transition-all duration-300`}
            />
            {!collapsed && (
              <>
                <span className="animate-fade-in text-xl font-bold text-sidebar-foreground">Inicio Insights</span>
              </>
            )}
          </div>
        </SidebarHeader>

        <SidebarContent className="px-2 py-4">
          <SidebarGroup className="p-3">
            {!collapsed && (
              <SidebarGroupLabel className="mb-4 px-1 text-sidebar-foreground/55 text-xs uppercase tracking-wider">
                Navigation
              </SidebarGroupLabel>
            )}
            <SidebarGroupContent>
              <SidebarMenu className={collapsed ? "mt-2 gap-5" : "gap-4"}>
                {renderGroupedSection({
                  groupKey: "overview",
                  item: overviewItem,
                  subItems: overviewSubItems,
                  isActive: isOverviewActive,
                })}
                {awarenessItem
                  ? renderGroupedSection({
                      groupKey: "awareness-usage",
                      item: awarenessItem,
                      subItems: awarenessSubItems,
                      isActive: isAwarenessSubpageActive,
                    })
                  : null}
                {itemsBetweenGroupedSections.map(renderNavItem)}
                {flavourFlexSectionItem
                  ? renderGroupedSection({
                      groupKey: "flavour-flex-section",
                      item: flavourFlexSectionItem,
                      subItems: flavourFlexSubItems,
                      isActive: isFlavourFlexSubpageActive,
                    })
                  : null}
                {hasCampaignCheckSubpages && campaignCheckItem
                  ? renderGroupedSection({
                      groupKey: "campaign-check",
                      item: campaignCheckItem,
                      subItems: campaignCheckSubItems,
                      isActive: isCampaignCheckSubpageActive,
                    })
                  : null}
                {itemsAfterGroupedSections.map(renderNavItem)}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
        </SidebarContent>

        <SidebarFooter className="p-3">
          {!collapsed && isAdmin && (
            <div className="glass-card p-3 mb-2 text-center border border-sidebar-border/30">
              <p className="mb-2 text-xs text-sidebar-foreground/65">Category</p>
              <select
                value={slug}
                onChange={(e) => handleAdminCategorySwitch(e.target.value)}
                className={`w-full rounded-md border border-sidebar-border/40 bg-sidebar/60 px-2 py-1.5 text-sm font-semibold text-sidebar-foreground outline-none ${categoryFocusBorderClass}`}
              >
                {adminCategories.map((item) => (
                  <option key={item.slug} value={item.slug}>
                    {item.category}
                  </option>
                ))}
              </select>
              <p className="mt-1 text-[11px] text-sidebar-foreground/70">{categoryLabel}</p>
            </div>
          )}
          <SidebarMenu>
            <SidebarMenuItem>
              <SidebarMenuButton
                onClick={handleLogout}
                tooltip="Logout"
                className={`rounded-xl text-sidebar-foreground/75 hover:bg-blue-500/20 hover:text-sidebar-foreground ${
                  collapsed ? "justify-center px-0 py-3.5" : "px-4 py-3"
                }`}
              >
                <LogOut className="h-5 w-5 shrink-0" />
                {!collapsed && <span className="text-[15px] font-medium">Logout</span>}
              </SidebarMenuButton>
            </SidebarMenuItem>
          </SidebarMenu>
        </SidebarFooter>
      </Sidebar>

      {isMobile && !openMobile && (
        <button
          type="button"
          onClick={() => setOpenMobile(true)}
          className="fixed left-3 top-3 z-50 rounded-full bg-sidebar-accent text-sidebar-foreground shadow-lg p-2"
          aria-label="Open sidebar"
        >
          <Menu className="h-5 w-5" />
        </button>
      )}
    </>
  );
}
