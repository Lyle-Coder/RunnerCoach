# -*- coding: utf-8 -*-
"""3D 赛道视频（P4）：DEM 地形 + 轨迹 + CP 标注，逐帧渲染成 MP4。

链路（复用 motion.py 的成熟做法）：
  GPX + terrarium DEM 瓦片
    → scene.json（瓦片 base64 内嵌，避免跨域与路径问题）
    → scene.html（WebGL 现场建 mesh + 相机关键帧）
    → 本地服务收帧（/__frame）
    → ffmpeg 合成

用法：
  python tools/make_3d.py                 # 完整导出
  python tools/make_3d.py --still 12.5    # 只渲染第 12.5 秒一张，用来调镜头
  python tools/make_3d.py --fps 24 --size 1280x720

注意：轨迹是 2026 届 105K（2027 尚未发布），画面右上角会常驻「2026 参考轨迹」。
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
OUT = ROOT / "output" / "3d"
GPX = ROOT / "data" / "events" / "panda-105k-2027" / "track.gpx"
FFMPEG = ROOT / "tools" / "ffmpeg.exe"
CHROME = "C:/Program Files/Google/Chrome/Application/chrome.exe"

Z = 12
G = "{http://www.topografix.com/GPX/1/1}"


# ---------------------------------------------------------------- 数据准备

def read_gpx(path: Path) -> tuple[list, list]:
    tree = ET.parse(path)
    pts, cps = [], []
    for p in tree.iter(G + "trkpt"):
        e = p.find(G + "ele")
        pts.append([float(p.get("lon")), float(p.get("lat")),
                    float(e.text) if e is not None else 0.0])
    for w in tree.iter(G + "wpt"):
        nm = w.find(G + "name")
        name = re.sub(r"\s+|\d+$", "", (nm.text or "")) if nm is not None else ""
        cps.append([float(w.get("lon")), float(w.get("lat")), name])
    return pts, cps


def tile_xy(lon: float, lat: float, z: int = Z) -> tuple[int, int]:
    n = 2 ** z
    x = int((lon + 180) / 360 * n)
    y = int((1 - math.log(math.tan(math.radians(lat)) + 1 / math.cos(math.radians(lat)))
             / math.pi) / 2 * n)
    return x, y


def ensure_tiles(pts: list) -> dict:
    xs, ys = zip(*[tile_xy(p[0], p[1]) for p in pts])
    x0, x1, y0, y1 = min(xs) - 1, max(xs) + 1, min(ys) - 1, max(ys) + 1
    tdir = OUT / "tiles"
    tdir.mkdir(parents=True, exist_ok=True)
    import urllib.request
    for tx in range(x0, x1 + 1):
        for ty in range(y0, y1 + 1):
            fp = tdir / ("%d_%d_%d.png" % (Z, tx, ty))
            if fp.exists():
                continue
            url = ("https://s3.amazonaws.com/elevation-tiles-prod/terrarium/%d/%d/%d.png"
                   % (Z, tx, ty))
            urllib.request.urlretrieve(url, fp)
    return {"z": Z, "x0": x0, "x1": x1, "y0": y0, "y1": y1}


def build_scene(pts: list, cps: list, meta: dict, ks: int = 0) -> dict:
    """瓦片 base64 内嵌；轨迹抽稀到 ~6000 点（线宽 12 m 的带子足够）。"""
    tiles = []
    tdir = OUT / "tiles"
    for ty in range(meta["y0"], meta["y1"] + 1):
        for tx in range(meta["x0"], meta["x1"] + 1):
            fp = tdir / ("%d_%d_%d.png" % (Z, tx, ty))
            tiles.append({
                "x": tx, "y": ty,
                "b64": base64.b64encode(fp.read_bytes()).decode(),
            })
    step = max(1, len(pts) // 6000)
    track = [[round(p[0], 6), round(p[1], 6), round(p[2])] for p in pts[::step]]
    if track[-1] != [round(pts[-1][0], 6), round(pts[-1][1], 6), round(pts[-1][2])]:
        track.append([round(pts[-1][0], 6), round(pts[-1][1], 6), round(pts[-1][2])])
    return {"tiles": tiles, "meta": meta, "track": track, "cps": cps,
            "label": "2026 参考轨迹"}


SCENE_HTML = r"""<!DOCTYPE html><html><head><meta charset="utf-8">
<style>
html,body{margin:0;background:#070B12;overflow:hidden;font-family:"Microsoft YaHei",sans-serif}
canvas{display:block}
#ov{position:absolute;left:0;top:0;pointer-events:none}
</style></head><body>
<canvas id="c"></canvas><canvas id="ov"></canvas>
<script>
const Q = new URLSearchParams(location.search);
const FPS = +(Q.get('fps') || 24);
const RUN = Q.get('run') || '';
const EXPORT = Q.get('export') === '1';
const STILL = Q.get('still') !== null ? +Q.get('still') : null;
let W = 1280, H = 720;
const c = document.getElementById('c'), ov = document.getElementById('ov');
// 导出时把 WebGL 画布与 2D 覆盖层合成一张：只 toBlob(WebGL) 会丢掉所有标签
const comp = document.createElement('canvas');
const cctx = comp.getContext('2d');
const gl = c.getContext('webgl', {antialias:true, preserveDrawingBuffer:true});
const ctx = ov.getContext('2d');

let scene = null, terrain = null, cam = null, shots = [], total = 0;
let curDist = 0;   // 当前镜头所在里程（米），进度尺用它而不是全局时间
let heightmap = null, HW = 0, HH = 0, mpp = 0, lat0 = 0, lon0 = 0;

function lat2y(lat){return Math.log(Math.tan(Math.PI/4 + lat*Math.PI/360));}

async function load(){
  scene = await (await fetch('scene.json')).json();
  document.title = 'loading';
  const m = scene.meta;
  const cols = m.x1-m.x0+1, rows = m.y1-m.y0+1;
  const T = 256;
  const off = document.createElement('canvas');
  off.width = cols*T; off.height = rows*T;
  const octx = off.getContext('2d');
  await Promise.all(scene.tiles.map(t => new Promise(res => {
    const im = new Image();
    im.onload = () => { octx.drawImage(im, (t.x-m.x0)*T, (t.y-m.y0)*T); res(); };
    im.onerror = () => res();
    im.src = 'data:image/png;base64,' + t.b64;
  })));
  // terrarium 解码：h = R*256 + G + B/256 - 32768
  const img = octx.getImageData(0, 0, off.width, off.height).data;
  HW = off.width; HH = off.height;
  heightmap = new Float32Array(HW*HH);
  for (let i=0, j=0; i<img.length; i+=4, j++)
    heightmap[j] = img[i]*256 + img[i+1] + img[i+2]/256 - 32768;

  // 原点取赛道中心；Web Mercator 在该纬度下的地面分辨率
  const lons = scene.track.map(p=>p[0]), lats = scene.track.map(p=>p[1]);
  lon0 = (Math.min(...lons)+Math.max(...lons))/2;
  lat0 = (Math.min(...lats)+Math.max(...lats))/2;
  mpp = 156543.03392 * Math.cos(lat0*Math.PI/180) / Math.pow(2, m.z);

  buildTerrain();
  buildTrack();
  buildShots();
  document.title = 'ready';
  if (STILL !== null) { renderAt(STILL); }
  else if (EXPORT) { exportFrames(); }
  else { loop(); }
}

// 世界坐标（米，东 x / 南 z）
function toWorld(lon, lat){
  return [ (lon-lon0)*111320*Math.cos(lat0*Math.PI/180),
           (lat-lat0)*110540 ];
}
function fromWorld(x, z){
  return [ lon0 + x/(111320*Math.cos(lat0*Math.PI/180)), lat0 + z/110540 ];
}
function px2lonlat(px, py){   // 拼接图素坐标 → 经纬度
  const m = scene.meta, n = Math.pow(2, m.z);
  const lon = ((m.x0 + px/HW* (m.x1-m.x0+1)) / n) * 360 - 180;
  const yy = (1 - (m.y0 + py/HH*(m.y1-m.y0+1)) / n);
  return [lon, ];
}
function sampleH(lon, lat){
  const m = scene.meta, n = Math.pow(2, m.z);
  const fx = ((lon+180)/360*n - m.x0) / (m.x1-m.x0+1) * HW;
  const fy = ((1 - lat2y(lat)/Math.PI)/2*n - m.y0) / (m.y1-m.y0+1) * HH;
  const x = Math.max(0, Math.min(HW-2, fx)), y = Math.max(0, Math.min(HH-2, fy));
  const x0 = Math.floor(x), y0 = Math.floor(y), dx = x-x0, dy = y-y0;
  const h00 = heightmap[y0*HW+x0], h10 = heightmap[y0*HW+x0+1];
  const h01 = heightmap[(y0+1)*HW+x0], h11 = heightmap[(y0+1)*HW+x0+1];
  return (h00*(1-dx)+h10*dx)*(1-dy) + (h01*(1-dx)+h11*dx)*dy;
}

const EX = 1.35;     // 高程夸张系数：读得清山形，又不至于像假山
function buildTerrain(){
  const m = scene.meta;
  const cols = m.x1-m.x0+1, rows = m.y1-m.y0+1;
  const N = 250;                       // 250×250=62.5k 顶点，16 位索引上限内
  const verts = [], idx = [], norm = [], col = [];
  const lonA = ((m.x0)/Math.pow(2,m.z))*360-180;
  const lonB = ((m.x1+1)/Math.pow(2,m.z))*360-180;
  const latA = latFromTileY(m.y0, m.z), latB = latFromTileY(m.y1+1, m.z);
  const hs = [];
  for (let j=0;j<=N;j++){
    for (let i=0;i<=N;i++){
      const lon = lonA + (lonB-lonA)*i/N;
      const lat = latA + (latB-latA)*j/N;
      const h = sampleH(lon, lat);
      hs.push(h);
      const w = toWorld(lon, lat);
      verts.push(w[0], h*EX, w[1]);
    }
  }
  for (let j=0;j<N;j++){
    for (let i=0;i<N;i++){
      const a=j*(N+1)+i, b=a+1, d=a+N+1, e=d+1;
      idx.push(a,d,b, b,d,e);
    }
  }
  // 法线 + 颜色：低处植被绿、中段土黄、高处岩灰；坡度越陡越亮
  const stepX = (lonB-lonA)*111320*Math.cos(lat0*Math.PI/180)/N;
  const stepZ = (latB-latA)*110540/N;
  for (let j=0;j<=N;j++){
    for (let i=0;i<=N;i++){
      const k=j*(N+1)+i;
      const hl = hs[j*(N+1)+Math.max(0,i-1)], hr = hs[j*(N+1)+Math.min(N,i+1)];
      const hu = hs[Math.max(0,j-1)*(N+1)+i], hd = hs[Math.min(N,j+1)*(N+1)+i];
      const nx = -(hr-hl)*EX/(2*stepX), nz = -(hd-hu)*EX/(2*stepZ);
      const len = Math.hypot(nx, 1, nz);
      norm.push(nx/len, 1/len, nz/len);
      const h = hs[k];
      const slope = Math.hypot(hr-hl, hd-hu)/Math.max(stepX,stepZ);
      // 自然色调：低处深绿 → 中段橄榄 → 高处灰岩；刻意压饱和，避免像沙丘
      let r,g,b;
      if (h < 800){ r=52; g=86; b=56; }
      else if (h < 1200){ const t=(h-800)/400; r=52+30*t; g=86+18*t; b=56+14*t; }
      else if (h < 1500){ const t=(h-1200)/300; r=82+22*t; g=104-6*t; b=70+6*t; }
      else if (h < 1720){ const t=(h-1500)/220; r=104+14*t; g=98+6*t; b=76+10*t; }
      else { const t=Math.min(1,(h-1720)/120); r=118+10*t; g=104+8*t; b=86+8*t; }
      // 局部起伏 + 确定性噪声：不然 DEM 平滑后成片的大坡会像一块布
      const rough = Math.abs(h - (hl+hr+hu+hd)/4);
      const rnd = Math.abs(Math.sin(i*12.9898 + j*78.233)) * 0.5;
      const tex = 1 - Math.min(0.22, rough/70) + (rnd - 0.25) * 0.09;
      const lit = (0.86 + Math.min(0.3, slope*0.55)) * tex;
      col.push(Math.min(196,r*lit)/255, Math.min(196,g*lit)/255, Math.min(196,b*lit)/255);
    }
  }
  terrain = makeMesh(verts, norm, col, idx);
}
function latFromTileY(y, z){
  const n = Math.PI - 2*Math.PI*y/Math.pow(2,z);
  return 180/Math.PI*Math.atan(0.5*(Math.exp(n)-Math.exp(-n)));
}

// ---- 最小 WebGL 封装
let prog, buf = {};
function shader(src, type){
  const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
  if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) console.error(gl.getShaderInfoLog(s));
  return s;
}
function initGL(){
  const vs = shader(`attribute vec3 aPos; attribute vec3 aNor; attribute vec3 aCol;
  uniform mat4 uMVP; varying vec3 vNor; varying vec3 vCol; varying vec3 vPos;
  void main(){ vNor=aNor; vCol=aCol; vPos=aPos; gl_Position=uMVP*vec4(aPos,1.0); }`, gl.VERTEX_SHADER);
  const fs = shader(`precision mediump float; varying vec3 vNor; varying vec3 vCol; varying vec3 vPos;
  uniform vec3 uLight; uniform vec3 uEye; uniform float uDim;
  void main(){
    float d = max(0.0, dot(normalize(vNor), normalize(uLight)));
    // amb+diffuse 必须 ≤1，否则迎光坡面直接过曝成一片奶白
    float amb = 0.30;
    vec3 c = vCol * (amb + 0.72*d) * uDim;
    // 远景雾：把 8 km 外的地形逐步混向天色，读出纵深
    float dist = distance(vPos, uEye);
    float f = smoothstep(8000.0, 46000.0, dist) * 0.62;
    c = mix(c, vec3(0.16,0.21,0.27), f);
    gl_FragColor = vec4(c, 1.0);
  }`, gl.FRAGMENT_SHADER);
  prog = gl.createProgram(); gl.attachShader(prog, vs); gl.attachShader(prog, fs);
  gl.linkProgram(prog); gl.useProgram(prog);
}
let skyProg = null, skyBuf = null;
function initSky(){
  const vs = shader(`attribute vec2 aP; varying float vY;
  void main(){ vY = aP.y*0.5+0.5; gl_Position = vec4(aP,0.0,1.0); }`, gl.VERTEX_SHADER);
  const fs = shader(`precision mediump float; varying float vY;
  void main(){
    vec3 top = vec3(0.031,0.055,0.098);
    vec3 mid = vec3(0.098,0.145,0.216);
    vec3 bot = vec3(0.239,0.294,0.353);
    vec3 c = mix(bot, mid, smoothstep(0.0,0.45,vY));
    c = mix(c, top, smoothstep(0.45,1.0,vY));
    gl_FragColor = vec4(c,1.0);
  }`, gl.FRAGMENT_SHADER);
  skyProg = gl.createProgram(); gl.attachShader(skyProg, vs); gl.attachShader(skyProg, fs);
  gl.linkProgram(skyProg);
  skyBuf = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, skyBuf);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1, 1,-1, -1,1, 1,1]), gl.STATIC_DRAW);
}
function drawSky(){
  gl.disable(gl.DEPTH_TEST);
  gl.useProgram(skyProg);
  gl.bindBuffer(gl.ARRAY_BUFFER, skyBuf);
  const aP = gl.getAttribLocation(skyProg, 'aP');
  gl.enableVertexAttribArray(aP);
  gl.vertexAttribPointer(aP, 2, gl.FLOAT, false, 0, 0);
  gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
  gl.enable(gl.DEPTH_TEST);
  gl.useProgram(prog);
}

function makeMesh(pos, nor, col, idx){
  const m = {};
  m.pos = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, m.pos);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(pos), gl.STATIC_DRAW);
  m.nor = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, m.nor);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(nor), gl.STATIC_DRAW);
  m.col = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, m.col);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array(col), gl.STATIC_DRAW);
  m.idx = gl.createBuffer(); gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, m.idx);
  gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, new Uint16Array(idx), gl.STATIC_DRAW);
  m.n = idx.length;
  return m;
}

// ---- 轨迹带子（把线扩成 14 m 宽的条，避免 WebGL 线宽限制）
let ribbon = null, trackW = [];
function buildTrack(){
  // 贴合地形：沿轨迹每 ~50 m 重采样一次 DEM（GPX 气压高度会浮空，只用两端
  // 高程又会把中间埋进山体——这是「轨迹断成虚线」的真正原因）
  trackW = scene.track.map(p => {
    const w = toWorld(p[0], p[1]);
    return [w[0], sampleH(p[0], p[1])*EX + 15, w[1]];
  });
  const W2 = 16, STEP = 45;
  const pos = [], idx = [], nor = [], col = [];
  const lift = 22;
  // 两侧边缘各自采样地形：横坡上若两条边共用一个中心高程，
  // 上坡侧会埋进山体，渲染出来就是一排锯齿缺口
  function edgeY(x, z){
    const ll = fromWorld(x, z);
    return sampleH(ll[0], ll[1])*EX + lift;
  }
  function pushQuad(a, b){
    const dx = b[0]-a[0], dz = b[2]-a[2];
    const len = Math.hypot(dx,dz) || 1;
    const nx = -dz/len*W2, nz = dx/len*W2;
    const base = pos.length/3;
    const ax1 = a[0]+nx, az1 = a[2]+nz, ax2 = a[0]-nx, az2 = a[2]-nz;
    const bx1 = b[0]+nx, bz1 = b[2]+nz, bx2 = b[0]-nx, bz2 = b[2]-nz;
    pos.push(ax1, edgeY(ax1, az1), az1,  ax2, edgeY(ax2, az2), az2,
             bx1, edgeY(bx1, bz1), bz1,  bx2, edgeY(bx2, bz2), bz2);
    for (let k=0;k<4;k++){ nor.push(0,1,0); col.push(1.0,0.34,0.06); }
    idx.push(base, base+1, base+2, base+1, base+3, base+2);
  }
  for (let i=0;i<trackW.length-1;i++){
    const a0 = trackW[i], b0 = trackW[i+1];
    const dx = b0[0]-a0[0], dz = b0[2]-a0[2];
    const len = Math.hypot(dx,dz);
    const n = Math.max(1, Math.ceil(len/STEP));
    let prev = a0;
    for (let k=1;k<=n;k++){
      const t = k/n;
      const x = a0[0]+dx*t, z = a0[2]+dz*t;
      const ll = fromWorld(x, z);
      const cur = [x, sampleH(ll[0], ll[1])*EX + lift, z];
      pushQuad(prev, cur);
      prev = cur;
    }
  }
  ribbon = makeMesh(pos, nor, col, idx);
}

// ---- 相机
function perspective(fovy, aspect, near, far){
  const f = 1/Math.tan(fovy/2), nf = 1/(near-far);
  return [f/aspect,0,0,0, 0,f,0,0, 0,0,(far+near)*nf,-1, 0,0,2*far*near*nf,0];
}
function lookAt(eye, at, up){
  const z = norm(sub(eye, at)), x = norm(cross(up, z)), y = cross(z, x);
  return [x[0],y[0],z[0],0, x[1],y[1],z[1],0, x[2],y[2],z[2],0,
          -dot(x,eye), -dot(y,eye), -dot(z,eye), 1];
}
const sub=(a,b)=>[a[0]-b[0],a[1]-b[1],a[2]-b[2]];
const dot=(a,b)=>a[0]*b[0]+a[1]*b[1]+a[2]*b[2];
const cross=(a,b)=>[a[1]*b[2]-a[2]*b[1], a[2]*b[0]-a[0]*b[2], a[0]*b[1]-a[1]*b[0]];
const norm=a=>{const l=Math.hypot(...a)||1; return [a[0]/l,a[1]/l,a[2]/l];};
function mul(a,b){
  const o=new Array(16);
  for(let i=0;i<4;i++)for(let j=0;j<4;j++){
    let s=0; for(let k=0;k<4;k++) s+=a[k*4+j]*b[i*4+k];
    o[i*4+j]=s;
  }
  return o;
}
const smooth = t => t*t*(3-2*t);

// 轨迹按里程插值
let cum = [];
function buildCum(){
  cum = [0];
  for (let i=1;i<trackW.length;i++){
    const a=trackW[i-1], b=trackW[i];
    cum.push(cum[i-1] + Math.hypot(b[0]-a[0], b[2]-a[2]));
  }
}
function atDist(d){
  let lo=0, hi=cum.length-1;
  while (lo<hi-1){ const mid=(lo+hi)>>1; if (cum[mid] <= d) lo=mid; else hi=mid; }
  const t = (d-cum[lo]) / Math.max(1, cum[hi]-cum[lo]);
  const a=trackW[lo], b=trackW[hi];
  return [a[0]+(b[0]-a[0])*t, a[1]+(b[1]-a[1])*t, a[2]+(b[2]-a[2])*t];
}
const TOTAL_M = () => cum[cum.length-1];

// ---- 镜头脚本（总时长由各段累加）
function cpDistances(){
  for (const cp of scene.cps){
    let best = 1e18, bi = 0;
    for (let i=0;i<trackW.length;i++){
      const dx = trackW[i][0]-toWorld(cp[0],cp[1])[0];
      const dz = trackW[i][2]-toWorld(cp[0],cp[1])[1];
      const d2 = dx*dx+dz*dz;
      if (d2 < best){ best = d2; bi = i; }
    }
    cp.km = cum[bi];
  }
}
function buildShots(){
  buildCum();
  cpDistances();
  const L = TOTAL_M();
  shots = [
    {kind:'orbit', dur:3.0},
    {kind:'follow', dur:5.0, from:0.00, to:0.10},     // 起点 → CP1 的爬升
    {kind:'follow', dur:5.0, from:0.16, to:0.20},     // CP2 前后的大下降
    {kind:'follow', dur:5.0, from:0.74, to:0.86},     // CP8→CP9 的夜爬
    {kind:'follow', dur:5.0, from:0.92, to:1.00},     // 最后下坡进城
    {kind:'night',  dur:4.0, from:0.80, to:0.93}      // 夜间段（时间轴 21:30 后）
  ];
  let t = 0;
  for (const s of shots){ s.t0 = t; t += s.dur; }
  total = t;
}

// 轨迹点已是 DEM 高度，直接用；再加一点余量避免贴着山脊
function sampleHAdjusted(p){ return p[1]; }
function cameraAt(t){
  const s = shots.find(x => t >= x.t0 && t < x.t0 + x.dur) || shots[shots.length-1];
  const p = Math.min(1, Math.max(0, (t - s.t0) / s.dur));
  const L = TOTAL_M();
  if (s.kind === 'orbit'){
    const k = smooth(p);
    const az = (-125 + 55*k) * Math.PI/180;
    const dist = 21000 - 4500*k;
    const ctr = atDist(L*0.5);
    curDist = L*0.5;
    const eye = [ctr[0] + Math.cos(az)*dist, 6200 - 1200*k, ctr[2] + Math.sin(az)*dist];
    return {eye, at: [ctr[0], ctr[1], ctr[2]], dim: 1.0};
  }
  const d = L * (s.from + (s.to - s.from) * smooth(p));
  curDist = d;
  const ahead = atDist(Math.min(L, d + 2200));
  const back = atDist(Math.max(0, d - 1900));
  // 相机必须比身后地形高，否则在陡坡段会钻进山体（看着像一大片绿色平面）
  const eyeY = Math.max(back[1] + 780, sampleHAdjusted(back) + 700);
  const eye = [back[0], eyeY, back[2]];
  const dim = s.kind === 'night' ? 0.45 : 1.0;
  return {eye, at: [ahead[0], ahead[1] + 60, ahead[2]], dim};
}

function renderAt(t){
  const shot = shots.find(x => t >= x.t0 && t < x.t0 + x.dur) || shots[0];
  cam = cameraAt(t);
  gl.viewport(0, 0, W, H);
  gl.clearColor(0.035, 0.055, 0.085, 1);
  gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
  gl.enable(gl.DEPTH_TEST);

  drawSky();
  const proj = perspective(48*Math.PI/180, W/H, 50, 120000);
  const view = lookAt(cam.eye, cam.at, [0,1,0]);
  const mvp = mul(proj, view);

  gl.uniformMatrix4fv(gl.getUniformLocation(prog,'uMVP'), false, new Float32Array(mvp));
  gl.uniform3fv(gl.getUniformLocation(prog,'uLight'), new Float32Array([-0.45, 0.82, 0.35]));
  gl.uniform1f(gl.getUniformLocation(prog,'uDim'), cam.dim);
  gl.uniform3fv(gl.getUniformLocation(prog,'uEye'), new Float32Array(cam.eye));

  bindMesh(terrain);
  gl.drawElements(gl.TRIANGLES, terrain.n, gl.UNSIGNED_SHORT, 0);

  // 轨迹：只画已经「到过」的部分（按当前镜头进度）
  const p = Math.min(1, Math.max(0, (t - shot.t0) / shot.dur));
  // 只有开场的鹰眼做「逐步画出」，跟拍段必须整条都在，否则镜头到了 75 km 却看不到线
  const frac = shot.kind === 'orbit' ? (0.15 + 0.85*smooth(p)) : 1.0;
  const n = Math.max(2, Math.floor(ribbon.n / 6 * frac) * 6);
  // 轨迹与地形几乎共面：靠 polygonOffset 把线「抬」到深度缓冲前面，
  // 否则会一段段被地形吃掉（叠起来像梳齿）
  gl.enable(gl.POLYGON_OFFSET_FILL);
  gl.polygonOffset(-4.0, -8.0);
  bindMesh(ribbon);
  gl.uniform1f(gl.getUniformLocation(prog,'uDim'), 1.7);
  gl.drawElements(gl.TRIANGLES, Math.min(ribbon.n, n), gl.UNSIGNED_SHORT, 0);
  gl.disable(gl.POLYGON_OFFSET_FILL);
  gl.uniform1f(gl.getUniformLocation(prog,'uDim'), cam.dim);

  drawOverlay(t, mvp, p);
}

function bindMesh(m){
  const aPos = gl.getAttribLocation(prog,'aPos'), aNor = gl.getAttribLocation(prog,'aNor'),
        aCol = gl.getAttribLocation(prog,'aCol');
  gl.bindBuffer(gl.ARRAY_BUFFER, m.pos); gl.enableVertexAttribArray(aPos);
  gl.vertexAttribPointer(aPos,3,gl.FLOAT,false,0,0);
  gl.bindBuffer(gl.ARRAY_BUFFER, m.nor); gl.enableVertexAttribArray(aNor);
  gl.vertexAttribPointer(aNor,3,gl.FLOAT,false,0,0);
  gl.bindBuffer(gl.ARRAY_BUFFER, m.col); gl.enableVertexAttribArray(aCol);
  gl.vertexAttribPointer(aCol,3,gl.FLOAT,false,0,0);
  gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, m.idx);
}

// ---- 2D 覆盖层：CP 标签、进度尺、水印
function project(p, mvp){
  const x = mvp[0]*p[0]+mvp[4]*p[1]+mvp[8]*p[2]+mvp[12];
  const y = mvp[1]*p[0]+mvp[5]*p[1]+mvp[9]*p[2]+mvp[13];
  const w = mvp[3]*p[0]+mvp[7]*p[1]+mvp[11]*p[2]+mvp[15];
  if (w <= 0) return null;
  return [(x/w*0.5+0.5)*W, (1-(y/w*0.5+0.5))*H];
}
function drawOverlay(t, mvp, p){
  ctx.clearRect(0,0,W,H);
  // CP 标签
  ctx.font = '12px "Microsoft YaHei"';
  const placed = [];
  const nearOnly = !(shots.find(x=>x.kind==='orbit') && t < 3.0);
  for (const cp of scene.cps){
    // 跟拍时不显示 5 km 外的 CP，否则整屏都是远处标签
    const lim = placed.length < 2 ? 14000 : 5000;
    if (nearOnly && Math.abs((cp.km||0) - curDist) > lim) continue;
    const w = toWorld(cp[0], cp[1]);
    const h = sampleH(cp[0], cp[1])*EX;
    const s = project([w[0], h+30, w[1]], mvp);
    if (!s || s[0]<0 || s[0]>W || s[1]<0 || s[1]>H) continue;
    // 已占位太近就跳过，避免标签糊成一团
    if (placed.some(q => Math.abs(q[0]-s[0]) < 74 && Math.abs(q[1]-s[1]) < 15)) continue;
    placed.push(s);
    const lift = (placed.length % 2) ? 20 : 0;   // 相邻标签上下错开
    ctx.fillStyle = 'rgba(6,12,20,.72)';
    const tw = ctx.measureText(cp[2]).width + 10;
    ctx.fillRect(s[0]-8, s[1]-22-lift, tw, 16);
    ctx.fillStyle = '#9FE7C8';
    ctx.fillText(cp[2], s[0]-3, s[1]-10-lift);
    ctx.fillStyle = '#FF7A29';
    ctx.beginPath(); ctx.arc(s[0], s[1], 3.2, 0, 7); ctx.fill();
  }
  // 左下：进度尺 + 当前海拔（里程取镜头位置，不是全局时间）
  const L = TOTAL_M(), d = curDist || L*(Math.min(1,t/total));
  const cur = atDist(d);
  ctx.fillStyle = 'rgba(6,12,20,.72)'; ctx.fillRect(28, H-86, 300, 58);
  ctx.fillStyle = '#E8F0FB'; ctx.font = 'bold 20px "Microsoft YaHei"';
  ctx.fillText((d/1000).toFixed(1) + ' km', 42, H-56);
  ctx.fillStyle = '#8FA3B8'; ctx.font = '13px "Microsoft YaHei"';
  ctx.fillText('海拔 ' + Math.round(cur[1]/EX) + ' m', 42, H-34);
  // 右上：标注
  ctx.fillStyle = 'rgba(6,12,20,.72)'; ctx.fillRect(W-218, 24, 194, 40);
  ctx.fillStyle = '#E8F0FB'; ctx.font = 'bold 14px "Microsoft YaHei"';
  ctx.fillText('熊猫蜀道山 105K', W-204, 42);
  ctx.fillStyle = '#D6A75A'; ctx.font = '12px "Microsoft YaHei"';
  ctx.fillText(scene.label + ' · 非比赛路线', W-204, 58);
}

// ---- 循环 / 导出
function loop(){
  const t0 = performance.now();
  const tick = () => {
    const t = ((performance.now()-t0)/1000) % total;
    renderAt(t);
    requestAnimationFrame(tick);
  };
  tick();
}
async function exportFrames(){
  const N = Math.round(total * FPS);
  const A = +(Q.get('from') || 0), B = Math.min(N, +(Q.get('to') || N));
  for (let i=A;i<B;i++){
    renderAt(i / FPS);
    cctx.drawImage(c, 0, 0);
    cctx.drawImage(ov, 0, 0);
    const blob = await new Promise(r => comp.toBlob(r, 'image/jpeg', 0.88));
    await fetch('/__frame?i=' + i + '&run=' + RUN, {method:'POST', body: blob});
    if (i % 12 === 0) document.title = 'frame ' + i + '/' + B;
  }
  await fetch('/__done?from=0&run=' + RUN, {method:'POST'});
  document.title = 'done';
}

function fit(w, h){
  W = w; H = h;
  c.width = W; c.height = H; ov.width = W; ov.height = H;
  comp.width = W; comp.height = H;
  c.style.width = W+'px'; c.style.height = H+'px';
  ov.style.width = W+'px'; ov.style.height = H+'px';
}
initGL();
initSky();
fit(+(Q.get('w')||1280), +(Q.get('h')||720));
load();
</script></body></html>
"""


# ---------------------------------------------------------------- 导出

def write_scene(pts, cps):
    meta = ensure_tiles(pts)
    scene = build_scene(pts, cps, meta)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "scene.json").write_text(json.dumps(scene), encoding="utf-8")
    (OUT / "scene.html").write_text(SCENE_HTML, encoding="utf-8")
    return scene


def serve(out_dir: Path, port: int):
    sys.path.insert(0, str(ROOT))
    from garmin_cn import serve as S
    srv = ThreadingHTTPServer(("127.0.0.1", port),
                              partial(S._Handler, directory=str(out_dir)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def run_still(sec: float, w: int, h: int):
    from garmin_cn import serve as S  # noqa: F401  (确保模块可用)
    serve(OUT.parent, 8795)
    out = OUT / ("still_%.1f.jpg" % sec)
    cmd = [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
           "--force-device-scale-factor=1",
           "--window-size=%d,%d" % (w, h),
           "--virtual-time-budget=9000",
           "--screenshot=%s" % out,
           "http://127.0.0.1:8795/3d/scene.html?still=%s&w=%d&h=%d" % (sec, w, h)]
    subprocess.run(cmd, timeout=120, capture_output=True)
    return out


def run_export(fps: int, w: int, h: int, keep: bool, chunk: int = 220):
    """分片渲染：Chrome 一次跑完整段容易超过命令行工具的 10 分钟上限，
    这里每片 ~220 帧（约 2–3 分钟），同一 run 目录续写。"""
    run_id = "course3d%d" % int(time.time())
    frames = OUT.parent / "_frames" / run_id
    frames.mkdir(parents=True, exist_ok=True)
    serve(OUT.parent, 8796)

    # 总帧数由页面脚本算出来同样一套（27 s × fps），这里先问一次页面太麻烦，
    # 直接用镜头脚本总时长 × fps
    total_frames = int(round(27.0 * fps))
    done = frames / "_done.json"
    a = 0
    while a < total_frames:
        b = min(total_frames, a + chunk)
        url = ("http://127.0.0.1:8796/3d/scene.html?export=1&run=%s&fps=%d&w=%d&h=%d"
               "&from=%d&to=%d" % (run_id, fps, w, h, a, b))
        if done.exists():
            done.unlink()
        proc = subprocess.Popen([CHROME, "--headless=new", "--disable-gpu",
                                 "--hide-scrollbars", "--mute-audio",
                                 "--force-device-scale-factor=1",
                                 "--window-size=%d,%d" % (w, h), url],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        t0 = time.time()
        while time.time() - t0 < 420:
            if done.exists():
                break
            if proc.poll() is not None and not done.exists():
                break
            time.sleep(0.5)
        time.sleep(0.5)
        try:
            proc.terminate()
        except Exception:
            pass
        got = len(list(frames.glob("f_*.jpg")))
        print("  片 %d-%d 完成，累计 %d/%d 帧" % (a, b, got, total_frames))
        if got < b:
            break
        a = b

    n = len(list(frames.glob("f_*.jpg")))
    out = ROOT / "output" / "plan-105k-3d.mp4"
    if n < 10:
        print("× 只收到 %d 帧，导出失败" % n)
        return None
    cmd = [str(FFMPEG), "-y", "-framerate", str(fps), "-i", str(frames / "f_%05d.jpg"),
           "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20",
           "-movflags", "+faststart", str(out)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("× ffmpeg 失败：", r.stderr[-400:])
        return None
    if not keep:
        for f in frames.glob("f_*.jpg"):
            f.unlink()
    return out, n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--still", type=float, help="只渲染某一秒的静帧")
    ap.add_argument("--fps", type=int, default=24)
    ap.add_argument("--size", default="1280x720")
    ap.add_argument("--keep-frames", action="store_true")
    a = ap.parse_args()
    w, h = (int(x) for x in a.size.lower().split("x"))

    pts, cps = read_gpx(GPX)
    scene = write_scene(pts, cps)
    print("场景：%d 点轨迹 / %d 个 CP / %d 张 DEM 瓦片"
          % (len(scene["track"]), len(scene["cps"]), len(scene["tiles"])))

    if a.still is not None:
        p = run_still(a.still, w, h)
        print("静帧:", p)
        return 0

    res = run_export(a.fps, w, h, a.keep_frames)
    if res:
        out, n = res
        mb = out.stat().st_size / 1024 / 1024
        print("√ 视频已生成：%s（%d 帧，%.1f MB）" % (out, n, mb))
    return 0


if __name__ == "__main__":
    sys.exit(main())
