/**
 * 首帧前写入 data-shell：宽屏桌面 = preview（模拟 iPhone），
 * 真机 / 窄屏 / standalone = native（全屏）。
 * 同时首帧同步写死 --sat/--sab/--nav-safe-top，避免 hydration 前安全区为 0 导致顶栏纵向跳。
 */
export const SHELL_BOOT_SCRIPT = `(function(){try{var d=document.documentElement;var alone=window.matchMedia("(display-mode: standalone)").matches||!!(navigator.standalone);var narrow=window.matchMedia("(max-width: 480px)").matches;var dark=window.matchMedia("(prefers-color-scheme: dark)").matches;var native=alone||narrow;d.dataset.shell=native?"native":"preview";d.dataset.standalone=alone?"1":"0";d.dataset.theme=dark?"dark":"light";d.style.colorScheme=dark?"dark":"light";if(alone){d.style.setProperty("--app-height","100vh");}else{d.style.removeProperty("--app-height");}if(native&&!alone&&!narrow){d.style.setProperty("--sat","max(env(safe-area-inset-top, 0px), 20px)");}if(!native){d.style.setProperty("--sat","44px");d.style.setProperty("--sab","34px");d.style.setProperty("--nav-safe-top","44px");}if(dark){d.style.backgroundColor="#000000";}else{d.style.backgroundColor="#f2f2f7";}}catch(e){document.documentElement.dataset.shell="native";}})();`;
