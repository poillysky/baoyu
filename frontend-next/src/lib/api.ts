/**
 * 鲍鱼盒子 API 客户端 — 对接 baoyu-py backend (server.py :8900)。
 * 后端是纯 stdlib http.server 的 REST 接口，统一 `{ok, data}` 或直接返回。
 */

export const API_BASE =
  typeof window === 'undefined'
    ? (process.env.API_INTERNAL_BASE || 'http://127.0.0.1:8900').replace(/\/$/, '')
    : (process.env.NEXT_PUBLIC_API_BASE || '').replace(/\/$/, '');

/** 封面/头像统一走后端代理，规避防盗链/证书/双栈差异。 */
export function imgUrl(raw?: string): string {
  if (!raw) return '';
  if (raw.startsWith('data:') || raw.startsWith('blob:')) return raw;
  return `${API_BASE}/api/img?url=${encodeURIComponent(raw)}`;
}

/** 统一 JSON 请求，返回解析后的 data（或整体对象）。 */
export async function apiGet<T = any>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { credentials: 'include' });
  if (!res.ok) throw new Error(`请求失败 ${res.status}`);
  return (await res.json()) as T;
}

// ── 类型 ──────────────────────────────────────────────

export interface Platform {
  id: number;
  plat_id: number;
  name: string;
  cover?: string;
  room_count: number;
  status?: number;
}

export interface Room {
  room_id: string;
  name?: string;
  title?: string;
  cover?: string;
  view_count?: number;
  online?: number;
  type?: number;
  pid?: number;
}

export interface RecItem {
  rec_id: string;
  room_id: string;
  plat_id: number;
  name: string;
  state: string;
  bytes: number;
  seg: number;
  seg_bytes: number;
  mp4?: string;
  segments?: unknown[];
  started_at?: number;
  retry?: number;
  /** 直录(ffmpeg直拉源流)标记: 直接录制引擎, 无浏览器开销 */
  mode?: 'direct' | 'web';
  engine?: 'direct_ffmpeg' | 'web_chromium';
}

export interface FileItem {
  name: string;
  folder: string;
  rel: string;
  size: number;
  mtime: number;
  url: string;
  thumb?: string;
  duration?: number;
}

export interface WatchItem {
  room_id: string;
  name: string;
  pid: number;
  plat_id?: number | string;
  online?: boolean;
  recording?: boolean;
  reason?: string;
  rec_state?: string | null;
  rec_id?: string | null;
  rec_started_at?: number | null;
  rec_duration?: number;
  rec_bytes?: number;
  checked_at?: number | null;
  last_msg?: string | null;
}

export interface AccountInfo {
  mobile?: string;
  email?: string;
  nickname?: string;
  login_type?: string;
  login_at?: number;
}

export interface LoginStatus {
  has_account: boolean;
  account_active: boolean;
  account_info?: AccountInfo;
}

// ── 领域 API ──────────────────────────────────────────

export function fetchPlatforms(): Promise<Platform[]> {
  return apiGet<{ ok: boolean; data: Platform[] }>('/api/platforms').then(
    (r) => (r && r.data) || [],
  );
}

export function fetchRooms(
  pid: number,
  page = 0,
  size = 0,
): Promise<{ data: Room[]; total: number; page: number; has_more: boolean }> {
  const q = `?pid=${pid}&page=${page}&size=${size}`;
  return apiGet<any>(`/api/rooms${q}`).then((r) => ({
    data: (r && r.data) || [],
    total: r?.total ?? 0,
    page: r?.page ?? 0,
    has_more: !!r?.has_more,
  }));
}

export function fetchRecStatus(): Promise<RecItem[]> {
  return apiGet<any>('/api/rec/status').then((r) => (r && r.data) || []);
}

export function fetchFiles(): Promise<FileItem[]> {
  return apiGet<any>('/api/files').then((r) => (r && r.data) || []);
}

export function fetchStats(): Promise<any> {
  return apiGet<any>('/api/stats').then((r) => r?.data ?? r);
}

export function fetchWatchList(): Promise<WatchItem[]> {
  return apiGet<{ ok: boolean; data: WatchItem[] }>('/api/watch/list').then(
    (r) => (r && r.data) || [],
  );
}

export function addWatch(roomId: string, pid: number, name: string) {
  const q = `?room_id=${encodeURIComponent(roomId)}&pid=${pid}&name=${encodeURIComponent(name)}`;
  return apiGet<any>(`/api/watch/add${q}`);
}

export function removeWatch(roomId: string) {
  return apiGet<any>(`/api/watch/remove?room_id=${encodeURIComponent(roomId)}`);
}

export function stopRec(recId: string) {
  return apiGet<any>(`/api/streamers/stop?rec_id=${encodeURIComponent(recId)}`);
}

export function startRec(roomId: string, pid: number, name: string) {
  const q = `?room_id=${encodeURIComponent(roomId)}&pid=${pid}&name=${encodeURIComponent(name)}`;
  return apiGet<any>(`/api/streamers/start${q}`);
}

export function fetchLoginStatus(): Promise<LoginStatus> {
  return apiGet<any>('/api/login/status').then((r) => r?.data ?? r);
}

export function sendCode(mobile: string) {
  return apiGet<any>(`/api/login/send_code?mobile=${encodeURIComponent(mobile)}`);
}

export function loginByAccount(mobile: string, password: string) {
  return apiGet<any>(
    `/api/login/account?mobile=${encodeURIComponent(mobile)}&password=${encodeURIComponent(password)}&login_type=mobile`,
  );
}

export function logout() {
  return apiGet<any>('/api/login/logout');
}

export function fetchHealth(): Promise<any> {
  return apiGet<any>('/api/health');
}

// ── 设置 ──────────────────────────────────────────────

export interface NetTestItem {
  name: string;
  ok: boolean;
  ms: number;
  msg: string;
}

export interface NetTestResult {
  results: NetTestItem[];
  proxy: { proxy_enabled: boolean; proxy_url: string };
  all_ok: boolean;
}

export interface SettingsAll {
  proxy: { proxy_enabled: boolean; proxy_url: string };
  modes: {
    single_browser: boolean;
    max_concurrent: number;
    transcode_fast: boolean;
    video_bitrate: number;
    capture_fps: number;
    quality?: 'high' | 'medium' | 'low';
  };
}

export function fetchSettingsAll(): Promise<SettingsAll> {
  return apiGet<any>('/api/settings/all').then((r) => r?.data ?? r);
}

export function setProxy(enabled: boolean, url?: string) {
  const q = `?enabled=${enabled ? '1' : '0'}${url ? `&url=${encodeURIComponent(url)}` : ''}`;
  return apiGet<any>(`/api/settings/proxy${q}`).then((r) => r?.data ?? r);
}

export function setMode(updates: {
  single_browser?: boolean;
  transcode_fast?: boolean;
  max_concurrent?: number;
  video_bitrate?: number;
  capture_fps?: number;
  quality?: 'high' | 'medium' | 'low';
}) {
  const p = new URLSearchParams();
  if (updates.single_browser !== undefined)
    p.set('single_browser', updates.single_browser ? '1' : '0');
  if (updates.transcode_fast !== undefined)
    p.set('transcode_fast', updates.transcode_fast ? '1' : '0');
  if (updates.max_concurrent !== undefined) p.set('max_concurrent', String(updates.max_concurrent));
  if (updates.video_bitrate !== undefined) p.set('video_bitrate', String(updates.video_bitrate));
  if (updates.capture_fps !== undefined) p.set('capture_fps', String(updates.capture_fps));
  if (updates.quality !== undefined) p.set('quality', updates.quality);
  return apiGet<any>(`/api/settings/mode?${p.toString()}`).then((r) => r?.data ?? r);
}

export function runNetTest(): Promise<NetTestResult> {
  return apiGet<any>('/api/settings/nettest').then((r) => r?.data ?? r);
}

export interface CacheStats {
  count: number;
  size_bytes: number;
  cleared?: number;
}

export function fetchCacheStats(): Promise<CacheStats> {
  return apiGet<any>('/api/settings/cache').then((r) => r?.data ?? r);
}

export function clearCache(): Promise<CacheStats> {
  return apiGet<any>('/api/settings/cache?clear=1').then((r) => r?.data ?? r);
}
