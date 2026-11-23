'use client';

import { useEffect, useState, useCallback } from 'react';
import { AppShell, TabProvider, TabPane } from '@/shell';
import { PlatformsScreen } from '@/features/platforms/PlatformsScreen';
import { RoomsScreen } from '@/features/rooms/RoomsScreen';
import { PlayScreen } from '@/features/play/PlayScreen';
import { WatchScreen } from '@/features/watch/WatchScreen';
import { FilesScreen } from '@/features/files/FilesScreen';
import { SettingsScreen } from '@/features/settings/SettingsScreen';
import { registerServiceWorker } from '@/lib/pwa';
import { DeviceFrame } from '@/components/layout/DeviceFrame';
import type { Platform, Room } from '@/lib/api';

/**
 * 主 Tab：平台（含房间/播放子页）是唯一带子页栈的 tab。
 * 其余 tab 都是单层 screen。
 */
export default function HomePage() {
  useEffect(() => {
    const cleanup = registerServiceWorker();
    return cleanup;
  }, []);

  return (
    <DeviceFrame label="鲍鱼直播台">
      <TabProvider>
        <AppShell>
          <TabPane tab="/">
            <PlatformsTab />
          </TabPane>
          <TabPane tab="/files">
            <FilesScreen />
          </TabPane>
          <TabPane tab="/watch">
            <WatchScreen />
          </TabPane>
          <TabPane tab="/settings">
            <SettingsScreen />
          </TabPane>
        </AppShell>
      </TabProvider>
    </DeviceFrame>
  );
}

/** 平台 tab 内部的子页栈：平台列表 → 房间列表 → 播放。 */
function PlatformsTab() {
  const [plat, setPlat] = useState<Platform | null>(null);
  const [room, setRoom] = useState<Room | null>(null);

  const openRooms = useCallback((p: Platform) => setPlat(p), []);
  const openPlay = useCallback((r: Room) => setRoom(r), []);
  const backToRooms = useCallback(() => setRoom(null), []);
  const backToPlats = useCallback(() => {
    setRoom(null);
    setPlat(null);
  }, []);

  if (room && plat) {
    return <PlayScreen room={{ ...room, pid: plat.id } as Room} onBack={backToRooms} />;
  }
  if (plat) {
    return (
      <RoomsScreen
        pid={plat.id}
        name={plat.name}
        onBack={backToPlats}
        onPlay={openPlay}
      />
    );
  }
  return <PlatformsScreen onOpen={openRooms} />;
}
