#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
密语原生录制 -> 对接 baoyu 项目流程测试

流程:
  1. 从 gateway 拿密语房凭据(appid/channel/token/uid/name/room_id)
  2. 调 NAS 上 Java 录制服务 POST /api/recording/startByRoom 录 N 秒
  3. 等录制完成(轮询 /api/recording/list 或按 duration 等)
  4. 从 NAS 平铺目录 /vol1/1000/Docker/agora-recorder/recordings/ 把新文件拉回本地
  5. 按项目规则建 recordings/{平台名}/{主播名_room_id}/{主播名}_{时间戳}.mp4 归位
  
这样 Java 服务只管"标准平铺录制", 目录/重命名/拉回全由项目侧做 —— 就是老大要的形态。
"""
import os, re, time, json, subprocess, sys

# ---- 项目侧配置 ----
NAS_SSH = "poilly@192.168.2.38"
NAS_PASS = "2885748zhu"
NAS_FLAT_DIR = "/vol1/1000/Docker/agora-recorder/recordings"   # NAS 容器挂载的平铺目录
JAVA_API = "http://192.168.2.38:8080"                          # NAS 上 Java 服务
PROJ_REC_DIR = "/Volumes/vol1T/project/cams/baoyu/baoyu-py/recordings"  # 项目本地录制根目录
PLAT_ID = 474
PLAT_NAME = "密语"

sys.path.insert(0, "/Volumes/vol1T/project/cams/baoyu/baoyu-py/backend")


def ssh(cmd, timeout=60):
    env = dict(os.environ, SSHPASS=NAS_PASS)
    # sudo -S 需要从 stdin 读密码; ls 目录属 root, 必须 sudo
    wrapped = f"echo '{NAS_PASS}' | sudo -S sh -c \"{cmd}\""
    return subprocess.run(["sshpass", "-e", "ssh", "-o", "StrictHostKeyChecking=no",
                           "-p", "22", NAS_SSH, wrapped],
                          input=NAS_PASS + "\n",
                          capture_output=True, text=True, timeout=timeout, env=env)


def scp_from(remote, local, timeout=120):
    env = dict(os.environ, SSHPASS=NAS_PASS)
    return subprocess.run(["sshpass", "-e", "scp", "-o", "StrictHostKeyChecking=no",
                           "-P", "22", f"{NAS_SSH}:{remote}", local],
                          capture_output=True, text=True, timeout=timeout, env=env)


def sanitize(s):
    return re.sub(r'[\\/:*?"<>|\s]+', "_", str(s or "")).strip("_")


def main():
    import client as CL, gateway
    room_id = sys.argv[1] if len(sys.argv) > 1 else "6516530"
    duration = int(sys.argv[2]) if len(sys.argv) > 2 else 20

    print(f"== 1) 取密语凭据 room={room_id} ==")
    CL.ensure_token(force=True)
    cred = gateway.get_stream_credentials(room_id, PLAT_ID)
    assert cred.get("ok") and cred.get("appid"), f"凭据失败: {cred}"
    name = cred.get("name") or f"ROOM_{room_id}"
    print(f"   主播={name} channel={cred['channel']} uid={cred['uid']}")

    print(f"== 2) 记录录制前 NAS 平铺目录已有文件 ==")
    before = set(x.strip() for x in ssh(f"ls -1 {NAS_FLAT_DIR} 2>/dev/null").stdout.replace(
        NAS_PASS, "").splitlines() if x.strip().endswith(".mp4"))
    print(f"   已有 {len(before)} 个 mp4")

    print(f"== 3) 调 Java 服务开录 (duration={duration}s) ==")
    body = {"appid": cred["appid"], "channel": cred["channel"], "token": cred["token"],
            "uid": cred["uid"], "duration": duration}
    r = subprocess.run(["sshpass", "-e", "ssh", "-o", "StrictHostKeyChecking=no", "-p", "22", NAS_SSH,
                        f"curl -s -m15 -X POST {JAVA_API}/api/recording/startByRoom "
                        f"-H 'Content-Type: application/json' -d '{json.dumps(body)}'"],
                       capture_output=True, text=True, env=dict(os.environ, SSHPASS=NAS_PASS))
    resp = json.loads(r.stdout)
    print("   ->", resp)
    assert resp.get("code") == 0, f"开录失败: {resp}"
    task_id = resp["taskId"]

    print(f"== 4) 等录制完成 (~duration+8s) ==")
    time.sleep(duration + 8)

    print(f"== 5) 找 NAS 上新增的 mp4 文件 ==")
    after = set(x.strip() for x in ssh(f"ls -1 {NAS_FLAT_DIR} 2>/dev/null").stdout.replace(
        NAS_PASS, "").splitlines() if x.strip().endswith(".mp4"))
    new_files = sorted(after - before)
    print(f"   新增: {new_files}")
    if not new_files:
        print("!! 没有新文件, 录制失败")
        return

    print(f"== 6) 按项目规则建多级目录 + 拉回本地 ==")
    plat_dir = sanitize(PLAT_NAME)
    anchor = f"{sanitize(name)}_{room_id}"
    out_dir = os.path.join(PROJ_REC_DIR, plat_dir, anchor)
    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    local_mp4 = os.path.join(out_dir, f"{sanitize(name)}_{stamp}.mp4")

    src = new_files[0]
    print(f"   拉取 {NAS_FLAT_DIR}/{src} -> {local_mp4}")
    res = scp_from(f"{NAS_FLAT_DIR}/{src}", local_mp4)
    if res.returncode != 0:
        print("!! scp 失败:", res.stderr[:300])
        return
    size = os.path.getsize(local_mp4)
    print(f"== 完成: 本地文件 {local_mp4} ({size} bytes) ==")

    # 校验
    probe = subprocess.run(["/opt/homebrew/bin/ffprobe", "-v", "error",
                            "-show_entries", "format=duration:stream=codec_name,codec_type,width,height",
                            "-of", "default=nw=1", local_mp4], capture_output=True, text=True)
    print("   ffprobe:\n" + probe.stdout)


if __name__ == "__main__":
    main()
