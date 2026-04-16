# -*- coding: utf-8 -*-
"""
================================================================================
  AC'S PVZ 0.1  -  Plants vs Zombies 1 Engine Recreation
  A.C Holdings / Team Flames  -  (c) 1999-2026
================================================================================
  Single-file pygame. 60 FPS. Windows XP-cadence tuning.

  FEATURES
  --------
  - Full PvZ 1 engine: sun economy, 5x9 lawn, wave progression, lawnmowers,
    seed packet cooldowns, plant/zombie collision, projectiles.
  - Plants:  Peashooter, Sunflower, Wall-nut, Cherry Bomb, Snow Pea,
             Potato Mine, Repeater, Chomper
  - Zombies: Basic, Conehead, Buckethead, Flag Zombie (wave marker)
  - Crazy Dave intro dialog (voiced)
  - "The Zombies Are Coming!" voiced wave announce (EN / ZH toggle, press L)
  - Plant & zombie callouts (voiced)
  - Unity-ported component system: GameObject / Transform / MonoBehaviour
  - TTS auto-fallback chain: macOS `say` -> pyttsx3 -> gTTS -> numpy synth
    (background thread, cached to OS tempdir, never blocks the game loop)
  - Text stays English; voice language togglable with L (EN <-> ZH)

  CONTROLS
  --------
  Mouse LMB  : pick seed packet -> click lawn cell to plant
               (click falling/on-ground suns to collect)
  Mouse RMB  : cancel selection
  Keys 1..8  : quick-pick seed packet
  S          : shovel toggle (click a plant to remove)
  L          : toggle voice language (EN <-> ZH)
  P          : pause
  ENTER      : advance Dave dialog / start / restart
  ESC        : back to title (from play) / quit (from title)

  REQUIRES
  --------
  python 3.11+, pygame, numpy
  optional:   pyttsx3, gTTS  (auto-detected; game runs fine without them)

  RUN
  ---
      pip install pygame numpy pyttsx3 gTTS
      python acs_pvz_0_1.py
================================================================================
"""

import os, sys, math, random, time, threading, subprocess, shutil, hashlib
import platform, tempfile, queue, wave
import pygame
import numpy as np


# ============================================================================ #
#  CONFIG                                                                      #
# ============================================================================ #

WIDTH, HEIGHT = 1000, 640
FPS           = 60
TITLE         = "AC'S PVZ 0.1  -  Team Flames  (c) 1999-2026"

LAWN_COLS   = 9
LAWN_ROWS   = 5
CELL_W      = 78
CELL_H      = 92
LAWN_X      = 230
LAWN_Y      = 110

# Team Flames palette
C_BG        = (6, 10, 16)
C_LAWN_A    = (62, 124, 44)
C_LAWN_B    = (82, 148, 58)
C_LAWN_EDGE = (34, 70, 24)
C_TEXT      = (220, 232, 255)
C_BLUE      = (77, 166, 255)
C_BLUE_DIM  = (42, 96, 160)
C_PANEL     = (14, 22, 36)
C_PANEL_HI  = (24, 38, 60)
C_SUN       = (255, 210, 60)
C_SUN_EDGE  = (220, 160, 20)
C_RED       = (220, 70, 70)
C_DARKRED   = (140, 30, 30)
C_WHITE     = (240, 240, 240)
C_PACKET    = (130, 88, 40)
C_PACKET_HI = (180, 130, 70)
C_SHADOW    = (0, 0, 0)

LANG_EN = "en"
LANG_ZH = "zh"


# ============================================================================ #
#  TTS ENGINE  -  AUTO-FALLBACK CHAIN                                          #
#  macOS `say`  ->  pyttsx3  ->  gTTS  ->  numpy synth                         #
# ============================================================================ #

class TTSEngine:
    """Non-blocking TTS with on-disk caching.
    Requests are queued; synthesis runs on a background thread.
    Once a .wav exists it is loaded as a pygame Sound and played.
    Cache key = md5(lang|text) -> <tempdir>/acs_pvz_tts/<key>.wav
    """
    def __init__(self):
        self.cache_dir = os.path.join(tempfile.gettempdir(), "acs_pvz_tts")
        os.makedirs(self.cache_dir, exist_ok=True)
        self.q        = queue.Queue()
        self.sounds   = {}
        self.pending  = set()
        self.lock     = threading.Lock()
        self.is_mac   = (platform.system() == "Darwin")
        self.has_say  = self.is_mac and shutil.which("say") is not None
        try:
            import pyttsx3  # noqa: F401
            self.has_pyttsx3 = True
        except Exception:
            self.has_pyttsx3 = False
        try:
            import gtts  # noqa: F401
            self.has_gtts = True
        except Exception:
            self.has_gtts = False

        self.worker = threading.Thread(target=self._loop, daemon=True)
        self.worker.start()
        print(f"[TTS] say={self.has_say}  pyttsx3={self.has_pyttsx3}  gtts={self.has_gtts}")

    # ---- public ---- #
    def speak(self, text, lang=LANG_EN, vol=0.9):
        key = self._key(text, lang)
        with self.lock:
            snd = self.sounds.get(key)
        if snd:
            try:
                ch = snd.play()
                if ch:
                    ch.set_volume(vol)
            except Exception:
                pass
            return
        with self.lock:
            if key in self.pending:
                return
            self.pending.add(key)
        self.q.put((text, lang, key, vol, True))

    def prewarm(self, pairs):
        for text, lang in pairs:
            key = self._key(text, lang)
            with self.lock:
                if key in self.sounds or key in self.pending:
                    continue
                self.pending.add(key)
            self.q.put((text, lang, key, 0.0, False))

    # ---- worker ---- #
    def _loop(self):
        while True:
            try:
                text, lang, key, vol, play_after = self.q.get()
            except Exception:
                continue
            wav_path = os.path.join(self.cache_dir, key + ".wav")
            if not os.path.exists(wav_path):
                ok = False
                try:
                    ok = self._synth_chain(text, lang, wav_path)
                except Exception as e:
                    print(f"[TTS] chain error: {e}")
                if not ok or not os.path.exists(wav_path):
                    try:
                        self._synth_numpy(text, lang, wav_path)
                    except Exception as e:
                        print(f"[TTS] numpy fallback error: {e}")
                        continue
            try:
                snd = pygame.mixer.Sound(wav_path)
                with self.lock:
                    self.sounds[key] = snd
                if play_after:
                    ch = snd.play()
                    if ch:
                        ch.set_volume(vol)
            except Exception as e:
                print(f"[TTS] load fail {key}: {e}")

    # ---- engine chain ---- #
    def _synth_chain(self, text, lang, out_wav):
        # 1) macOS say
        if self.has_say:
            try:
                aiff = out_wav + ".aiff"
                voice = "Samantha" if lang == LANG_EN else "Tingting"
                cmd = ["say", "-o", aiff, "-v", voice, text]
                r = subprocess.run(cmd, capture_output=True, timeout=20)
                if r.returncode != 0:
                    # try without voice flag (system default)
                    r = subprocess.run(["say", "-o", aiff, text],
                                       capture_output=True, timeout=20)
                if r.returncode == 0 and os.path.exists(aiff):
                    if shutil.which("afconvert"):
                        subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16",
                                        aiff, out_wav], capture_output=True, timeout=10)
                    else:
                        os.replace(aiff, out_wav)
                    try:
                        if os.path.exists(aiff):
                            os.remove(aiff)
                    except Exception:
                        pass
                    if os.path.exists(out_wav):
                        return True
            except Exception as e:
                print(f"[TTS] say failed: {e}")

        # 2) pyttsx3
        if self.has_pyttsx3:
            try:
                import pyttsx3
                eng = pyttsx3.init()
                try:
                    for v in eng.getProperty("voices"):
                        vid = (v.id or "").lower()
                        nm  = (v.name or "").lower()
                        if lang == LANG_ZH and ("zh" in vid or "chinese" in nm or "mandarin" in nm):
                            eng.setProperty("voice", v.id); break
                        if lang == LANG_EN and ("en" in vid or "english" in nm):
                            eng.setProperty("voice", v.id); break
                except Exception:
                    pass
                eng.setProperty("rate", 175)
                eng.save_to_file(text, out_wav)
                eng.runAndWait()
                if os.path.exists(out_wav):
                    return True
            except Exception as e:
                print(f"[TTS] pyttsx3 failed: {e}")

        # 3) gTTS
        if self.has_gtts:
            try:
                from gtts import gTTS
                mp3 = out_wav + ".mp3"
                tts = gTTS(text=text, lang=("zh-CN" if lang == LANG_ZH else "en"))
                tts.save(mp3)
                converted = False
                if shutil.which("ffmpeg"):
                    r = subprocess.run(["ffmpeg", "-y", "-i", mp3, out_wav],
                                       capture_output=True, timeout=25)
                    converted = (r.returncode == 0)
                if not converted and shutil.which("afconvert"):
                    r = subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16",
                                        mp3, out_wav], capture_output=True, timeout=15)
                    converted = (r.returncode == 0)
                if not converted:
                    # pygame can load mp3 on most platforms
                    os.replace(mp3, out_wav)
                else:
                    try:
                        if os.path.exists(mp3):
                            os.remove(mp3)
                    except Exception:
                        pass
                return os.path.exists(out_wav)
            except Exception as e:
                print(f"[TTS] gTTS failed: {e}")
        return False

    # ---- numpy last-resort ---- #
    def _synth_numpy(self, text, lang, out_wav):
        sr = 22050
        words = text.replace("!", " !").replace(",", " ,").replace(".", " .").split()
        segs = []
        for w in words:
            if w in ("!", ",", "."):
                segs.append(np.zeros(int(sr*0.12)))
                continue
            dur = max(0.18, min(0.45, 0.05*len(w) + 0.12))
            n = int(sr*dur)
            t = np.arange(n)/sr
            base = 140 if lang == LANG_EN else 170
            vib  = 6 if lang == LANG_EN else 10
            f = base + vib*np.sin(2*np.pi*5*t)
            phase = 2*np.pi*np.cumsum(f)/sr
            wave_ = 0.6*np.sin(phase) + 0.2*np.sign(np.sin(phase))
            nz = 0.08*np.random.uniform(-1,1,n)
            y = np.zeros(n); a = 0.25
            for i in range(1,n):
                y[i] = a*nz[i] + (1-a)*y[i-1]
            seg = wave_ + y
            env = np.ones(n)
            at = int(0.02*sr); rl = int(0.05*sr)
            env[:at] = np.linspace(0,1,at)
            env[-rl:] = np.linspace(1,0,rl)
            segs.append(seg*env)
            segs.append(np.zeros(int(sr*0.04)))
        y = np.concatenate(segs) if segs else np.zeros(int(sr*0.2))
        y = y / (np.max(np.abs(y))+1e-6) * 0.85
        y = (y*32767).astype(np.int16)
        with wave.open(out_wav, "wb") as wf:
            wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(sr)
            wf.writeframes(y.tobytes())

    def _key(self, text, lang):
        h = hashlib.md5(f"{lang}|{text}".encode("utf-8")).hexdigest()[:16]
        return f"{lang}_{h}"


# ============================================================================ #
#  UNITY-PORTED COMPONENT SYSTEM                                               #
# ============================================================================ #

class Transform:
    __slots__ = ("x", "y", "rot", "sx", "sy")
    def __init__(self, x=0, y=0):
        self.x = x; self.y = y; self.rot = 0.0; self.sx = 1.0; self.sy = 1.0

class GameObject:
    _id = 0
    def __init__(self, name="GO"):
        GameObject._id += 1
        self.id   = GameObject._id
        self.name = name
        self.transform = Transform()
        self.components = []
        self.active = True
        self.scene  = None

    def add(self, comp):
        comp.gameObject = self
        comp.transform  = self.transform
        self.components.append(comp)
        comp.Start()
        return comp

    def get(self, cls):
        for c in self.components:
            if isinstance(c, cls):
                return c
        return None

    def Update(self, dt):
        if not self.active:
            return
        for c in self.components:
            if c.enabled:
                c.Update(dt)

    def Draw(self, surf):
        if not self.active:
            return
        for c in self.components:
            if c.enabled:
                c.Draw(surf)

    def Destroy(self):
        self.active = False
        if self.scene:
            self.scene.destroy(self)


class MonoBehaviour:
    def __init__(self):
        self.gameObject = None
        self.transform  = None
        self.enabled    = True
    def Start(self): pass
    def Update(self, dt): pass
    def Draw(self, surf): pass


class Scene:
    def __init__(self):
        self.objects = []
        self._pending_add = []
        self._pending_del = []
        # type-indexed caches, rebuilt when dirty
        self._by_type = {}
        self._dirty   = True

    def add(self, go):
        go.scene = self
        self._pending_add.append(go)
        self._dirty = True
        return go

    def destroy(self, go):
        self._pending_del.append(go)
        self._dirty = True

    def flush(self):
        changed = False
        if self._pending_add:
            self.objects.extend(self._pending_add)
            self._pending_add.clear()
            changed = True
        if self._pending_del:
            s = set(self._pending_del)
            self.objects = [o for o in self.objects if o not in s]
            self._pending_del.clear()
            changed = True
        if changed:
            self._dirty = True

    def _rebuild_index(self):
        self._by_type = {}
        for o in self.objects:
            if not o.active:
                continue
            for c in o.components:
                self._by_type.setdefault(type(c), []).append(c)
        self._dirty = False

    def find_all(self, cls):
        if self._dirty:
            self._rebuild_index()
        # match subclass chain
        out = []
        for t, lst in self._by_type.items():
            if issubclass(t, cls):
                out.extend(lst)
        return out

    def Update(self, dt):
        self.flush()
        for o in list(self.objects):
            o.Update(dt)
        self.flush()
        self._dirty = True  # positions changed; caches still valid but membership fine

    def Draw(self, surf):
        for o in sorted(self.objects, key=lambda g: g.transform.y):
            o.Draw(surf)


# ============================================================================ #
#  HELPERS                                                                     #
# ============================================================================ #

def lane_y(row): return LAWN_Y + row*CELL_H + CELL_H//2
def col_x(col):  return LAWN_X + col*CELL_W + CELL_W//2

def cell_rect(col, row):
    return pygame.Rect(LAWN_X + col*CELL_W, LAWN_Y + row*CELL_H, CELL_W, CELL_H)

def which_cell(pos):
    mx, my = pos
    if mx < LAWN_X or my < LAWN_Y:
        return None
    c = (mx - LAWN_X) // CELL_W
    r = (my - LAWN_Y) // CELL_H
    if 0 <= c < LAWN_COLS and 0 <= r < LAWN_ROWS:
        return (int(c), int(r))
    return None


# ============================================================================ #
#  PROJECTILES                                                                 #
# ============================================================================ #

class Pea(MonoBehaviour):
    def __init__(self, row, dmg=20, frozen=False):
        super().__init__()
        self.row = row
        self.dmg = dmg
        self.frozen = frozen
        self.speed = 280.0  # px/sec
    def Update(self, dt):
        self.transform.x += self.speed * dt
        if self.transform.x > WIDTH + 20:
            self.gameObject.Destroy(); return
        scene = self.gameObject.scene
        for z in scene.find_all(Zombie):
            if z.row != self.row:
                continue
            if not z.gameObject.active:
                continue
            dx = z.transform.x - self.transform.x
            dy = z.transform.y - self.transform.y
            if -22 < dx < 22 and -30 < dy < 30:
                z.hit(self.dmg, frozen=self.frozen)
                self.gameObject.Destroy()
                return
    def Draw(self, surf):
        col = (140, 200, 255) if self.frozen else (140, 230, 100)
        pygame.draw.circle(surf, col, (int(self.transform.x), int(self.transform.y)), 6)
        pygame.draw.circle(surf, (40, 80, 30), (int(self.transform.x), int(self.transform.y)), 6, 1)


# ============================================================================ #
#  PLANTS                                                                      #
# ============================================================================ #

class Plant(MonoBehaviour):
    COST     = 100
    HP       = 100
    COOLDOWN = 7.5
    NAME     = "Plant"
    COLOR    = (120, 200, 100)
    EDGE     = (40, 80, 40)
    def __init__(self, col, row):
        super().__init__()
        self.col = col
        self.row = row
        self.hp  = self.HP
        self.t   = 0.0
    def Start(self):
        self.transform.x = col_x(self.col)
        self.transform.y = lane_y(self.row)
    def Update(self, dt):
        self.t += dt
    def hit(self, dmg):
        self.hp -= dmg
        if self.hp <= 0:
            self.gameObject.Destroy()
    def Draw(self, surf):
        cx, cy = int(self.transform.x), int(self.transform.y)
        pygame.draw.circle(surf, self.EDGE, (cx, cy + 4), 22)
        pygame.draw.circle(surf, self.COLOR, (cx, cy + 2), 20)
        pygame.draw.circle(surf, (0, 0, 0), (cx - 6, cy), 3)
        pygame.draw.circle(surf, (0, 0, 0), (cx + 6, cy), 3)


class Peashooter(Plant):
    COST = 100; HP = 100; COOLDOWN = 7.5
    NAME = "Peashooter"
    COLOR = (90, 180, 80); EDGE = (30, 70, 30)
    FIRE_EVERY = 1.4
    def __init__(self, col, row):
        super().__init__(col, row)
        self.fire_t = 0.0
    def Update(self, dt):
        super().Update(dt)
        if self._zombie_in_row():
            self.fire_t += dt
            if self.fire_t >= self.FIRE_EVERY:
                self.fire_t = 0.0
                self._shoot()
        else:
            self.fire_t = max(0.0, self.fire_t - dt*0.5)
    def _zombie_in_row(self):
        for z in self.gameObject.scene.find_all(Zombie):
            if z.row == self.row and z.transform.x > self.transform.x - 10 and z.transform.x < WIDTH:
                return True
        return False
    def _shoot(self):
        go = GameObject("Pea")
        go.transform.x = self.transform.x + 18
        go.transform.y = self.transform.y - 4
        go.add(Pea(self.row, dmg=20, frozen=False))
        self.gameObject.scene.add(go)
    def Draw(self, surf):
        super().Draw(surf)
        cx, cy = int(self.transform.x), int(self.transform.y)
        pygame.draw.circle(surf, self.EDGE, (cx + 18, cy - 2), 6)
        pygame.draw.circle(surf, self.COLOR, (cx + 18, cy - 2), 5)


class Repeater(Peashooter):
    COST = 200; NAME = "Repeater"
    COLOR = (70, 160, 70); EDGE = (20, 60, 20)
    FIRE_EVERY = 1.4
    def __init__(self, col, row):
        super().__init__(col, row)
        self._second_pending = 0.0
    def Update(self, dt):
        super().Update(dt)
        if self._second_pending > 0:
            self._second_pending -= dt
            if self._second_pending <= 0:
                go = GameObject("Pea")
                go.transform.x = self.transform.x + 18
                go.transform.y = self.transform.y - 4
                go.add(Pea(self.row, dmg=20))
                self.gameObject.scene.add(go)
    def _shoot(self):
        super()._shoot()
        self._second_pending = 0.18


class SnowPea(Peashooter):
    COST = 175; NAME = "Snow Pea"
    COLOR = (140, 200, 230); EDGE = (40, 90, 120)
    def _shoot(self):
        go = GameObject("IcePea")
        go.transform.x = self.transform.x + 18
        go.transform.y = self.transform.y - 4
        go.add(Pea(self.row, dmg=20, frozen=True))
        self.gameObject.scene.add(go)


class Sunflower(Plant):
    COST = 50; HP = 100; COOLDOWN = 7.5
    NAME = "Sunflower"
    COLOR = (255, 210, 60); EDGE = (200, 140, 20)
    def __init__(self, col, row):
        super().__init__(col, row)
        self.sun_t = random.uniform(5, 9)
    def Update(self, dt):
        super().Update(dt)
        self.sun_t -= dt
        if self.sun_t <= 0:
            self.sun_t = random.uniform(18, 24)
            self.gameObject.scene.add(
                make_sun(self.transform.x, self.transform.y - 10, natural=False))
    def Draw(self, surf):
        cx, cy = int(self.transform.x), int(self.transform.y)
        # petals
        for a in range(0, 360, 40):
            rad = math.radians(a + (self.t*20) % 40)
            px = cx + int(math.cos(rad)*20)
            py = cy + int(math.sin(rad)*20) + 2
            pygame.draw.circle(surf, self.EDGE, (px, py), 7)
            pygame.draw.circle(surf, self.COLOR, (px, py), 6)
        pygame.draw.circle(surf, (120, 80, 20), (cx, cy + 2), 12)
        pygame.draw.circle(surf, (60, 40, 10), (cx, cy + 2), 12, 2)


class WallNut(Plant):
    COST = 50; HP = 400; COOLDOWN = 20
    NAME = "Wall-nut"
    COLOR = (180, 130, 70); EDGE = (90, 60, 30)
    def Draw(self, surf):
        cx, cy = int(self.transform.x), int(self.transform.y)
        pygame.draw.ellipse(surf, self.EDGE, (cx-22, cy-20, 44, 44))
        pygame.draw.ellipse(surf, self.COLOR, (cx-20, cy-18, 40, 40))
        # eyes / expression based on HP
        if self.hp > self.HP * 0.66:
            pygame.draw.circle(surf, (0,0,0), (cx-6, cy), 2)
            pygame.draw.circle(surf, (0,0,0), (cx+6, cy), 2)
        elif self.hp > self.HP * 0.33:
            pygame.draw.line(surf, (0,0,0), (cx-9, cy-1), (cx-3, cy+1), 2)
            pygame.draw.line(surf, (0,0,0), (cx+3, cy-1), (cx+9, cy+1), 2)
            pygame.draw.line(surf, (60,30,10), (cx-14, cy+10), (cx+14, cy+10), 2)
        else:
            pygame.draw.line(surf, (0,0,0), (cx-10, cy-3), (cx-2, cy+3), 2)
            pygame.draw.line(surf, (0,0,0), (cx-10, cy+3), (cx-2, cy-3), 2)
            pygame.draw.line(surf, (0,0,0), (cx+2, cy-3), (cx+10, cy+3), 2)
            pygame.draw.line(surf, (0,0,0), (cx+2, cy+3), (cx+10, cy-3), 2)


class CherryBomb(Plant):
    COST = 150; HP = 9999; COOLDOWN = 30
    NAME = "Cherry Bomb"
    COLOR = (220, 50, 50); EDGE = (120, 20, 20)
    def __init__(self, col, row):
        super().__init__(col, row)
        self.fuse = 1.2
        self.exploded = False
    def Update(self, dt):
        super().Update(dt)
        self.fuse -= dt
        if self.fuse <= 0 and not self.exploded:
            self.exploded = True
            cx, cy = self.col, self.row
            for z in self.gameObject.scene.find_all(Zombie):
                if abs(z.row - cy) <= 1:
                    zc = int((z.transform.x - LAWN_X) // CELL_W)
                    if abs(zc - cx) <= 1:
                        z.hit(1800)
            self.gameObject.Destroy()
    def Draw(self, surf):
        cx, cy = int(self.transform.x), int(self.transform.y)
        pygame.draw.circle(surf, self.EDGE, (cx-6, cy+2), 14)
        pygame.draw.circle(surf, self.EDGE, (cx+6, cy+2), 14)
        pygame.draw.circle(surf, self.COLOR, (cx-6, cy), 13)
        pygame.draw.circle(surf, self.COLOR, (cx+6, cy), 13)
        pygame.draw.line(surf, (60,100,30), (cx-2, cy-14), (cx+4, cy-22), 2)
        if self.fuse < 0.5:
            r = int(30 + (0.5 - self.fuse)*80)
            pygame.draw.circle(surf, (255,180,60), (cx, cy), r, 3)


class PotatoMine(Plant):
    COST = 25; HP = 100; COOLDOWN = 30
    NAME = "Potato Mine"
    COLOR = (150, 100, 60); EDGE = (70, 45, 25)
    def __init__(self, col, row):
        super().__init__(col, row)
        self.arm = 14.0
        self.armed = False
    def Update(self, dt):
        super().Update(dt)
        self.arm -= dt
        if self.arm <= 0:
            self.armed = True
        if self.armed:
            for z in self.gameObject.scene.find_all(Zombie):
                if z.row == self.row and abs(z.transform.x - self.transform.x) < 26:
                    z.hit(1800)
                    self.gameObject.Destroy()
                    return
    def Draw(self, surf):
        cx, cy = int(self.transform.x), int(self.transform.y)
        if not self.armed:
            pygame.draw.ellipse(surf, (100, 70, 40), (cx-14, cy+6, 28, 14))
            pygame.draw.ellipse(surf, (60, 40, 20), (cx-14, cy+6, 28, 14), 2)
            # hole + timer dots
            pygame.draw.circle(surf, (40,30,20), (cx, cy+2), 4)
        else:
            pygame.draw.ellipse(surf, self.EDGE, (cx-18, cy-6, 36, 22))
            pygame.draw.ellipse(surf, self.COLOR, (cx-16, cy-4, 32, 18))
            pygame.draw.circle(surf, (255,80,80), (cx, cy-12), 5)
            pygame.draw.circle(surf, (160,30,30), (cx, cy-12), 5, 2)


class Chomper(Plant):
    COST = 150; HP = 300; COOLDOWN = 7.5
    NAME = "Chomper"
    COLOR = (180, 60, 150); EDGE = (90, 20, 80)
    def __init__(self, col, row):
        super().__init__(col, row)
        self.chew = 0.0
    def Update(self, dt):
        super().Update(dt)
        if self.chew > 0:
            self.chew -= dt
            return
        for z in self.gameObject.scene.find_all(Zombie):
            if z.row == self.row and 0 <= (z.transform.x - self.transform.x) < 60:
                z.hit(1800)
                self.chew = 8.0
                return
    def Draw(self, surf):
        cx, cy = int(self.transform.x), int(self.transform.y)
        if self.chew > 0:
            # chewing - closed mouth blob
            pygame.draw.ellipse(surf, self.EDGE, (cx-22, cy-10, 44, 28))
            pygame.draw.ellipse(surf, self.COLOR, (cx-20, cy-8, 40, 24))
        else:
            pygame.draw.ellipse(surf, self.EDGE, (cx-22, cy-14, 44, 32))
            pygame.draw.ellipse(surf, self.COLOR, (cx-20, cy-12, 40, 28))
            # mouth
            pygame.draw.polygon(surf, (30, 0, 20),
                                [(cx+4, cy-8),(cx+22, cy-14),(cx+22, cy+8),(cx+4, cy+4)])
            # teeth
            for tx in range(cx+6, cx+22, 4):
                pygame.draw.polygon(surf, C_WHITE,
                                    [(tx, cy-8),(tx+2, cy-4),(tx+4, cy-8)])


PLANT_CLASSES = [Peashooter, Sunflower, WallNut, CherryBomb,
                 SnowPea, PotatoMine, Repeater, Chomper]


# ============================================================================ #
#  ZOMBIES                                                                     #
# ============================================================================ #

class Zombie(MonoBehaviour):
    HP = 200
    SPEED = 14          # px/sec
    DMG_PER_SEC = 100
    NAME = "Zombie"
    COLOR = (130, 180, 130); EDGE = (50, 80, 50)
    def __init__(self, row):
        super().__init__()
        self.row = row
        self.hp  = self.HP
        self.base_speed = self.SPEED
        self.freeze_t = 0.0
        self.bob = random.uniform(0, 6.28)
        self.dead = False
    def Start(self):
        self.transform.x = WIDTH + 30
        self.transform.y = lane_y(self.row)
    def hit(self, dmg, frozen=False):
        if self.dead: return
        self.hp -= dmg
        if frozen:
            self.freeze_t = 3.0
        if self.hp <= 0:
            self.dead = True
            self.gameObject.Destroy()
    def _find_target(self):
        for p in self.gameObject.scene.find_all(Plant):
            if p.row == self.row and abs(p.transform.x - self.transform.x) < 30:
                return p
        return None
    def Update(self, dt):
        if self.dead: return
        self.bob += dt*6
        sp = self.base_speed * (0.5 if self.freeze_t > 0 else 1.0)
        if self.freeze_t > 0:
            self.freeze_t -= dt
        tgt = self._find_target()
        if tgt:
            tgt.hit(self.DMG_PER_SEC * dt)
        else:
            self.transform.x -= sp * dt
    def Draw(self, surf):
        cx = int(self.transform.x)
        cy = int(self.transform.y + math.sin(self.bob)*2)
        # body
        pygame.draw.rect(surf, self.EDGE, (cx-12, cy-4, 24, 34))
        pygame.draw.rect(surf, self.COLOR, (cx-10, cy-2, 20, 30))
        # head
        pygame.draw.circle(surf, self.EDGE, (cx, cy-14), 12)
        pygame.draw.circle(surf, self.COLOR, (cx, cy-14), 10)
        pygame.draw.circle(surf, (0,0,0), (cx-3, cy-15), 2)
        pygame.draw.circle(surf, (0,0,0), (cx+3, cy-15), 2)
        pygame.draw.line(surf, (80,0,0), (cx-4, cy-9), (cx+4, cy-9), 1)
        # arms out
        pygame.draw.line(surf, self.EDGE, (cx-10, cy+2), (cx-22, cy+2), 4)
        pygame.draw.line(surf, self.EDGE, (cx+10, cy+2), (cx+22, cy+2), 4)
        if self.freeze_t > 0:
            s = pygame.Surface((44, 54), pygame.SRCALPHA)
            s.fill((120, 180, 255, 70))
            surf.blit(s, (cx-22, cy-26))


class ConeheadZombie(Zombie):
    HP = 560
    NAME = "Conehead Zombie"
    def Draw(self, surf):
        super().Draw(surf)
        cx = int(self.transform.x)
        cy = int(self.transform.y + math.sin(self.bob)*2) - 14
        pygame.draw.polygon(surf, (200, 120, 50),
                            [(cx-9, cy-8),(cx+9, cy-8),(cx, cy-26)])
        pygame.draw.polygon(surf, (120, 60, 20),
                            [(cx-9, cy-8),(cx+9, cy-8),(cx, cy-26)], 2)


class BucketheadZombie(Zombie):
    HP = 1370
    NAME = "Buckethead Zombie"
    def Draw(self, surf):
        super().Draw(surf)
        cx = int(self.transform.x)
        cy = int(self.transform.y + math.sin(self.bob)*2) - 14
        pygame.draw.rect(surf, (150, 150, 160), (cx-11, cy-24, 22, 18))
        pygame.draw.rect(surf, (70, 70, 80), (cx-11, cy-24, 22, 18), 2)
        pygame.draw.ellipse(surf, (60, 60, 70), (cx-11, cy-8, 22, 6))


class FlagZombie(Zombie):
    HP = 200
    SPEED = 20
    NAME = "Flag Zombie"
    def Draw(self, surf):
        super().Draw(surf)
        cx = int(self.transform.x)
        cy = int(self.transform.y + math.sin(self.bob)*2)
        pygame.draw.line(surf, (80, 60, 40), (cx+14, cy-24), (cx+14, cy+6), 2)
        pygame.draw.polygon(surf, (220, 60, 60),
                            [(cx+14, cy-24),(cx+32, cy-18),(cx+14, cy-14)])


# ============================================================================ #
#  SUN TOKENS                                                                  #
# ============================================================================ #

class Sun(MonoBehaviour):
    def __init__(self, target_y, natural=True):
        super().__init__()
        self.target_y = target_y
        self.natural  = natural
        self.t        = 0.0
        self.life     = 9.0
        self.collected = False
    def Update(self, dt):
        if self.collected:
            self.transform.x += (30 - self.transform.x) * min(1.0, dt*8)
            self.transform.y += (30 - self.transform.y) * min(1.0, dt*8)
            if abs(self.transform.x - 30) < 8 and abs(self.transform.y - 30) < 8:
                self.gameObject.Destroy()
            return
        if self.transform.y < self.target_y:
            self.transform.y += 40 * dt
        self.life -= dt
        if self.life <= 0:
            self.gameObject.Destroy()
        self.t += dt
    def Draw(self, surf):
        cx, cy = int(self.transform.x), int(self.transform.y)
        # glow rays
        glow = 14 + int(2*math.sin(self.t*5))
        pygame.draw.circle(surf, C_SUN_EDGE, (cx, cy), glow+4)
        pygame.draw.circle(surf, C_SUN, (cx, cy), glow)
        pygame.draw.circle(surf, (255, 235, 140), (cx-3, cy-3), max(3, glow-8))


def make_sun(x, y, natural=True):
    go = GameObject("Sun")
    if natural:
        go.transform.x = x
        go.transform.y = -20
        target_y = random.randint(LAWN_Y + 40, LAWN_Y + LAWN_ROWS*CELL_H - 40)
    else:
        go.transform.x = x
        go.transform.y = y
        target_y = y
    go.add(Sun(target_y=target_y, natural=natural))
    return go


# ============================================================================ #
#  GAME STATE                                                                  #
# ============================================================================ #

# Chinese translations for plant names (spoken)
ZH_PLANT_VOICE = {
    "Peashooter":  "豌豆射手!",
    "Sunflower":   "向日葵!",
    "Wall-nut":    "坚果墙!",
    "Cherry Bomb": "樱桃炸弹!",
    "Snow Pea":    "寒冰射手!",
    "Potato Mine": "土豆雷!",
    "Repeater":    "双发射手!",
    "Chomper":     "大嘴花!",
}


class Game:
    def __init__(self):
        pygame.mixer.pre_init(22050, -16, 1, 512)
        pygame.init()
        pygame.display.set_caption(TITLE)
        self.screen  = pygame.display.set_mode((WIDTH, HEIGHT))
        self.clock   = pygame.time.Clock()
        self.font_sm = pygame.font.SysFont("consolas", 13)
        self.font    = pygame.font.SysFont("consolas", 17, bold=True)
        self.font_lg = pygame.font.SysFont("consolas", 26, bold=True)
        self.font_xl = pygame.font.SysFont("consolas", 46, bold=True)

        self.tts  = TTSEngine()
        self.lang = LANG_EN
        self._prewarm_voices()

        self._reset_run()

    def _reset_run(self):
        self.state  = "TITLE"
        self.paused = False
        self.sun    = 150
        self.grid   = [[None]*LAWN_COLS for _ in range(LAWN_ROWS)]
        self.scene  = Scene()
        self.selected_packet = -1
        self.shovel = False
        self.lawnmowers       = [True]*LAWN_ROWS
        self.lawnmower_pos    = [LAWN_X - 30]*LAWN_ROWS
        self.lawnmower_active = [False]*LAWN_ROWS
        self.packets = [dict(cls=cls, cd=0.0) for cls in PLANT_CLASSES]

        self.wave          = 0
        self.total_waves   = 10
        self.spawn_t       = 15.0    # first wave delay after Dave
        self.wave_active   = False
        self.wave_pool     = 0
        self.sky_sun_t     = 6.0

        self.dave_lines = [
            "HIIIIII!  I'm Crazy Dave!",
            "I'm CRA-AZY for protectin' my brains!",
            "Zombies are coming! Plant your peashooters!",
            "Press L to switch voice language.  WABBY WABBO!",
        ]
        self.dave_idx = 0
        self.announce      = ""
        self.announce_t    = 0.0

    # ---- voice helpers ---- #
    def _prewarm_voices(self):
        en = [
            "The zombies are coming!",
            "A huge wave of zombies is approaching!",
            "Final wave!",
            "You won! You saved the brains!",
            "The zombies ate your brains!",
            "Voice set to English.",
            "Peashooter!", "Sunflower!", "Wall-nut!", "Cherry Bomb!",
            "Snow Pea!", "Potato Mine!", "Repeater!", "Chomper!",
        ]
        zh = [
            "僵尸来了!", "一大波僵尸正在接近!", "最后一波!",
            "你赢了! 你保护了你的脑子!", "僵尸吃掉了你的脑子!",
            "语音已设置为中文。",
            "豌豆射手!", "向日葵!", "坚果墙!", "樱桃炸弹!",
            "寒冰射手!", "土豆雷!", "双发射手!", "大嘴花!",
        ]
        self.tts.prewarm([(t, LANG_EN) for t in en] + [(t, LANG_ZH) for t in zh])
        self.tts.prewarm([(l, LANG_EN) for l in self.dave_lines] if hasattr(self, "dave_lines")
                         else [])

    def say(self, en, zh=None, force_lang=None):
        lang = force_lang if force_lang else self.lang
        text = zh if (lang == LANG_ZH and zh) else en
        self.tts.speak(text, lang=lang)

    def announce_banner(self, text, seconds=2.5):
        self.announce = text
        self.announce_t = seconds

    # ---- packet / plant actions ---- #
    def select_packet(self, i):
        if i < 0 or i >= len(self.packets): return
        pk = self.packets[i]
        if pk["cd"] > 0: return
        if self.sun < pk["cls"].COST: return
        self.selected_packet = i
        self.shovel = False

    def try_plant(self, cell):
        if self.selected_packet < 0 or not cell: return
        c, r = cell
        if self.grid[r][c] is not None: return
        pk = self.packets[self.selected_packet]
        if self.sun < pk["cls"].COST: return
        if pk["cd"] > 0: return
        go = GameObject(pk["cls"].NAME)
        plant = pk["cls"](c, r)
        go.add(plant)
        self.scene.add(go)
        self.grid[r][c] = plant
        self.sun -= pk["cls"].COST
        pk["cd"] = pk["cls"].COOLDOWN
        # voiced callout
        name = pk["cls"].NAME
        self.say(f"{name}!", ZH_PLANT_VOICE.get(name))
        self.selected_packet = -1

    def try_shovel(self, cell):
        if not cell: return
        c, r = cell
        p = self.grid[r][c]
        if p:
            p.gameObject.Destroy()
            self.grid[r][c] = None
        self.shovel = False

    def click_sun(self, pos):
        best = None; best_d = 10**9
        for o in list(self.scene.objects):
            s = o.get(Sun)
            if s and not s.collected:
                dx = pos[0] - o.transform.x
                dy = pos[1] - o.transform.y
                d = dx*dx + dy*dy
                if d < 24*24 and d < best_d:
                    best_d = d; best = s
        if best:
            best.collected = True
            self.sun += 25
            return True
        return False

    # ---- waves ---- #
    def start_wave(self):
        self.wave += 1
        big   = (self.wave % 5 == 0) and (self.wave != self.total_waves)
        final = (self.wave == self.total_waves)
        base_pool = 2 + self.wave
        self.wave_pool = base_pool + (6 if big or final else 0)
        self.wave_active = True
        self.spawn_t = 0.4
        if final:
            self.say("Final wave!", "最后一波!")
            self.announce_banner("FINAL WAVE!", 3.0)
        elif big:
            self.say("A huge wave of zombies is approaching!", "一大波僵尸正在接近!")
            self.announce_banner("A HUGE WAVE APPROACHES", 3.0)
            go = GameObject("FlagZombie")
            go.add(FlagZombie(random.randint(0, LAWN_ROWS-1)))
            self.scene.add(go)
            self.wave_pool = max(1, self.wave_pool - 1)
        else:
            self.say("The zombies are coming!", "僵尸来了!")
            self.announce_banner("The Zombies Are Coming!", 2.2)

    def spawn_zombie(self):
        r = random.randint(0, LAWN_ROWS-1)
        tier = min(self.wave // 3, 2)
        roll = random.random()
        if tier == 0 or roll < 0.6:
            cls = Zombie
        elif tier == 1 or roll < 0.88:
            cls = ConeheadZombie
        else:
            cls = BucketheadZombie
        go = GameObject(cls.NAME)
        go.add(cls(r))
        self.scene.add(go)

    # ---- main update ---- #
    def update(self, dt):
        if self.announce_t > 0:
            self.announce_t -= dt

        if self.state != "PLAY" or self.paused:
            return

        # packet cooldowns
        for pk in self.packets:
            if pk["cd"] > 0:
                pk["cd"] = max(0.0, pk["cd"] - dt)

        # natural sun
        self.sky_sun_t -= dt
        if self.sky_sun_t <= 0:
            self.sky_sun_t = random.uniform(7, 11)
            x = random.randint(LAWN_X + 20, LAWN_X + LAWN_COLS*CELL_W - 20)
            self.scene.add(make_sun(x, 0, natural=True))

        # scene tick
        self.scene.Update(dt)

        # grid cleanup (destroyed plants)
        for r in range(LAWN_ROWS):
            for c in range(LAWN_COLS):
                p = self.grid[r][c]
                if p is not None and (not p.gameObject.active or p.hp <= 0):
                    self.grid[r][c] = None

        # waves
        if not self.wave_active:
            self.spawn_t -= dt
            if self.spawn_t <= 0 and self.wave < self.total_waves:
                self.start_wave()
        else:
            self.spawn_t -= dt
            if self.spawn_t <= 0 and self.wave_pool > 0:
                self.spawn_zombie()
                self.wave_pool -= 1
                self.spawn_t = random.uniform(1.4, 2.8) if self.wave_pool > 0 else 0
            if self.wave_pool <= 0 and not self.scene.find_all(Zombie):
                self.wave_active = False
                if self.wave >= self.total_waves:
                    self.state = "WIN"
                    self.say("You won! You saved the brains!",
                             "你赢了! 你保护了你的脑子!")
                else:
                    self.spawn_t = 10.0

        # lawnmowers trigger + movement
        for z in list(self.scene.find_all(Zombie)):
            if not z.gameObject.active: continue
            if z.transform.x < LAWN_X - 6:
                row = z.row
                if self.lawnmowers[row]:
                    self.lawnmowers[row] = False
                    self.lawnmower_active[row] = True
        for r in range(LAWN_ROWS):
            if self.lawnmower_active[r]:
                self.lawnmower_pos[r] += 520 * dt
                for z in list(self.scene.find_all(Zombie)):
                    if z.row == r and abs(z.transform.x - self.lawnmower_pos[r]) < 26:
                        z.hit(9999)
                if self.lawnmower_pos[r] > WIDTH + 30:
                    self.lawnmower_active[r] = False

        # loss check
        for z in list(self.scene.find_all(Zombie)):
            if not z.gameObject.active: continue
            if z.transform.x < LAWN_X - 40 \
               and not self.lawnmowers[z.row] \
               and not self.lawnmower_active[z.row]:
                self.state = "LOSE"
                self.say("The zombies ate your brains!",
                         "僵尸吃掉了你的脑子!")
                break


    # ---- drawing ---- #
    def draw_lawn(self, surf):
        # checker lawn
        for r in range(LAWN_ROWS):
            for c in range(LAWN_COLS):
                col = C_LAWN_A if (r + c) % 2 == 0 else C_LAWN_B
                pygame.draw.rect(surf, col,
                                 (LAWN_X + c*CELL_W, LAWN_Y + r*CELL_H, CELL_W, CELL_H))
        pygame.draw.rect(surf, C_LAWN_EDGE,
                         (LAWN_X - 1, LAWN_Y - 1,
                          LAWN_COLS*CELL_W + 2, LAWN_ROWS*CELL_H + 2), 2)
        # house on the left
        pygame.draw.rect(surf, (60, 40, 30),
                         (LAWN_X - 60, LAWN_Y, 55, LAWN_ROWS*CELL_H))
        pygame.draw.polygon(surf, (100, 40, 30),
                            [(LAWN_X - 60, LAWN_Y),
                             (LAWN_X - 5,  LAWN_Y),
                             (LAWN_X - 32, LAWN_Y - 30)])
        for r in range(LAWN_ROWS):
            pygame.draw.rect(surf, (30, 20, 15),
                             (LAWN_X - 50, LAWN_Y + r*CELL_H + 24, 36, 2))
        # lawnmowers
        for r in range(LAWN_ROWS):
            x = self.lawnmower_pos[r]
            if self.lawnmowers[r] or self.lawnmower_active[r]:
                y = lane_y(r)
                pygame.draw.rect(surf, (180, 40, 40), (x-16, y-8, 32, 16))
                pygame.draw.rect(surf, (120, 20, 20), (x-16, y-8, 32, 16), 2)
                pygame.draw.circle(surf, (30, 30, 30), (int(x-10), int(y+10)), 5)
                pygame.draw.circle(surf, (30, 30, 30), (int(x+10), int(y+10)), 5)
                pygame.draw.rect(surf, (220, 220, 220), (x-2, y-14, 4, 8))

    def draw_ui(self, surf):
        # top panel
        pygame.draw.rect(surf, C_PANEL, (0, 0, WIDTH, 90))
        pygame.draw.line(surf, C_BLUE_DIM, (0, 90), (WIDTH, 90), 2)

        # sun bank
        pygame.draw.circle(surf, C_SUN_EDGE, (30, 30), 18)
        pygame.draw.circle(surf, C_SUN, (30, 30), 16)
        t = self.font_lg.render(str(self.sun), True, C_TEXT)
        surf.blit(t, (60, 14))

        # packets
        for i, pk in enumerate(self.packets):
            x = 180 + i*86; y = 8
            r = pygame.Rect(x, y, 80, 72)
            bg = C_PACKET_HI if i == self.selected_packet else C_PACKET
            pygame.draw.rect(surf, bg, r, border_radius=6)
            pygame.draw.rect(surf, (60, 40, 20), r, 2, border_radius=6)
            pygame.draw.circle(surf, pk["cls"].COLOR, (x + 40, y + 28), 15)
            pygame.draw.circle(surf, pk["cls"].EDGE,  (x + 40, y + 28), 15, 2)
            name = pk["cls"].NAME
            t = self.font_sm.render(name[:10], True, C_TEXT)
            surf.blit(t, (x + 4, y + 48))
            t2 = self.font_sm.render(str(pk["cls"].COST), True, C_SUN)
            surf.blit(t2, (x + 4, y + 60))
            # hotkey
            hk = self.font_sm.render(str(i+1), True, C_BLUE)
            surf.blit(hk, (x + r.width - 10, y + 2))
            # shade by cooldown/affordability
            if pk["cd"] > 0:
                h = int(72 * (pk["cd"]/pk["cls"].COOLDOWN))
                s = pygame.Surface((80, h), pygame.SRCALPHA); s.fill((0, 0, 0, 130))
                surf.blit(s, (x, y))
            elif self.sun < pk["cls"].COST:
                s = pygame.Surface((80, 72), pygame.SRCALPHA); s.fill((0, 0, 0, 90))
                surf.blit(s, (x, y))

        # shovel button
        sx = 180 + len(self.packets)*86 + 8
        sr = pygame.Rect(sx, 8, 60, 72)
        pygame.draw.rect(surf, (80, 80, 90) if not self.shovel else C_BLUE_DIM,
                         sr, border_radius=6)
        pygame.draw.rect(surf, (40, 40, 50), sr, 2, border_radius=6)
        pygame.draw.polygon(surf, (170, 170, 180),
                            [(sx + 18, sr.y + 12),
                             (sx + 42, sr.y + 12),
                             (sx + 30, sr.y + 54)])
        t = self.font_sm.render("SHOVEL S", True, C_TEXT)
        surf.blit(t, (sx - 2, sr.bottom + 2))

        # footer
        pygame.draw.rect(surf, C_PANEL, (0, HEIGHT - 26, WIDTH, 26))
        pygame.draw.line(surf, C_BLUE_DIM, (0, HEIGHT - 26), (WIDTH, HEIGHT - 26), 1)
        footer = (f"A.C HOLDINGS / TEAM FLAMES  (c) 1999-2026   |   "
                  f"Voice: {self.lang.upper()}   |   "
                  f"Wave {self.wave}/{self.total_waves}   |   "
                  f"L=lang  P=pause  ESC=title")
        t = self.font_sm.render(footer, True, C_BLUE)
        surf.blit(t, (8, HEIGHT - 20))

    def draw_title(self, surf):
        surf.fill(C_BG)
        # starfield
        for i in range(90):
            random.seed(i*7 + 3)
            x = random.randint(0, WIDTH)
            y = random.randint(0, HEIGHT - 60)
            col = 40 + random.randint(0, 90)
            pygame.draw.circle(surf, (col, col, col+10), (x, y), 1)
        random.seed()
        t = self.font_xl.render("AC'S PVZ 0.1", True, C_BLUE)
        surf.blit(t, (WIDTH//2 - t.get_width()//2, 140))
        t2 = self.font_lg.render("Plants vs Zombies - Team Flames Recreation", True, C_TEXT)
        surf.blit(t2, (WIDTH//2 - t2.get_width()//2, 200))
        lines = [
            "[ENTER]  Begin  (Crazy Dave is waiting...)",
            f"[L]      Toggle voice language (EN / ZH)    current: {self.lang.upper()}",
            "[ESC]    Quit",
            "",
            "A.C HOLDINGS / TEAM FLAMES   (c) 1999-2026",
        ]
        for i, line in enumerate(lines):
            r = self.font.render(line, True, C_BLUE if i == 0 else C_TEXT)
            surf.blit(r, (WIDTH//2 - r.get_width()//2, 300 + i*30))

    def draw_dave(self, surf):
        self.draw_lawn(surf)
        self.scene.Draw(surf)
        self.draw_ui(surf)
        s = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA); s.fill((0, 0, 0, 140))
        surf.blit(s, (0, 0))

        box = pygame.Rect(60, HEIGHT - 210, WIDTH - 120, 150)
        pygame.draw.rect(surf, C_PANEL, box, border_radius=10)
        pygame.draw.rect(surf, C_BLUE, box, 2, border_radius=10)
        # dave portrait
        pygame.draw.circle(surf, (220, 180, 140), (120, box.y + 80), 40)
        pygame.draw.circle(surf, (80, 50, 30), (120, box.y + 80), 40, 3)
        pygame.draw.rect(surf, (80, 50, 30), (85, box.y + 35, 70, 22))
        # saucepan "hat"
        pygame.draw.arc(surf, (180, 180, 190),
                        (88, box.y + 15, 64, 44), 3.3, 6.1, 6)
        pygame.draw.circle(surf, (0, 0, 0), (108, box.y + 75), 3)
        pygame.draw.circle(surf, (0, 0, 0), (132, box.y + 75), 3)
        pygame.draw.arc(surf, (80, 30, 30),
                        (100, box.y + 85, 40, 20), 3.4, 6.0, 3)
        # name
        n = self.font_lg.render("CRAZY DAVE", True, C_BLUE)
        surf.blit(n, (180, box.y + 12))
        # line
        line = self.dave_lines[self.dave_idx]
        self._wrap_blit(surf, line, self.font, C_TEXT,
                        pygame.Rect(180, box.y + 52, box.w - 200, 80))
        # prompt
        p = self.font_sm.render("[SPACE / ENTER] next", True, C_BLUE)
        surf.blit(p, (box.right - 150, box.bottom - 22))

    def _wrap_blit(self, surf, text, font, color, rect):
        words = text.split(" ")
        line = ""; y = rect.y
        for w in words:
            test = (line + " " + w).strip()
            if font.size(test)[0] > rect.w:
                surf.blit(font.render(line, True, color), (rect.x, y))
                y += font.get_height() + 2; line = w
            else:
                line = test
        if line:
            surf.blit(font.render(line, True, color), (rect.x, y))

    def draw_overlay(self, surf, text, color):
        s = pygame.Surface((WIDTH, HEIGHT), pygame.SRCALPHA); s.fill((0, 0, 0, 170))
        surf.blit(s, (0, 0))
        t = self.font_xl.render(text, True, color)
        surf.blit(t, (WIDTH//2 - t.get_width()//2, HEIGHT//2 - 40))
        hint = self.font.render("[ENTER] Title   [ESC] Quit", True, C_TEXT)
        surf.blit(hint, (WIDTH//2 - hint.get_width()//2, HEIGHT//2 + 20))

    def draw_announce(self, surf):
        if self.announce_t <= 0 or not self.announce:
            return
        alpha = min(1.0, self.announce_t/0.6) if self.announce_t < 0.6 else 1.0
        s = pygame.Surface((WIDTH, 70), pygame.SRCALPHA)
        s.fill((0, 0, 0, int(140 * alpha)))
        surf.blit(s, (0, HEIGHT//2 - 70))
        t = self.font_xl.render(self.announce, True, C_BLUE)
        tt = t.copy()
        tt.set_alpha(int(255 * alpha))
        surf.blit(tt, (WIDTH//2 - tt.get_width()//2, HEIGHT//2 - 60))


    # ---- event handling ---- #
    def handle(self, ev):
        if ev.type == pygame.QUIT:
            pygame.quit(); sys.exit(0)

        if ev.type == pygame.KEYDOWN:
            # global language toggle
            if ev.key == pygame.K_l:
                self.lang = LANG_ZH if self.lang == LANG_EN else LANG_EN
                self.say("Voice set to English.", "语音已设置为中文。")
                return

            if self.state == "TITLE":
                if ev.key == pygame.K_RETURN:
                    self.state = "DAVE"
                    self.dave_idx = 0
                    self.tts.speak(self.dave_lines[0], LANG_EN)
                elif ev.key == pygame.K_ESCAPE:
                    pygame.quit(); sys.exit(0)

            elif self.state == "DAVE":
                if ev.key in (pygame.K_SPACE, pygame.K_RETURN):
                    self.dave_idx += 1
                    if self.dave_idx >= len(self.dave_lines):
                        self.state = "PLAY"
                        self.spawn_t = 12.0
                    else:
                        self.tts.speak(self.dave_lines[self.dave_idx], LANG_EN)

            elif self.state == "PLAY":
                if ev.key == pygame.K_p:
                    self.paused = not self.paused
                elif ev.key == pygame.K_ESCAPE:
                    self._reset_run(); return
                elif ev.key == pygame.K_s:
                    self.shovel = not self.shovel
                    if self.shovel:
                        self.selected_packet = -1
                elif pygame.K_1 <= ev.key <= pygame.K_8:
                    self.select_packet(ev.key - pygame.K_1)

            elif self.state in ("WIN", "LOSE"):
                if ev.key == pygame.K_RETURN:
                    self._reset_run()
                elif ev.key == pygame.K_ESCAPE:
                    pygame.quit(); sys.exit(0)

        if ev.type == pygame.MOUSEBUTTONDOWN and self.state == "PLAY" and not self.paused:
            if ev.button == 1:
                # UI: packet click
                for i, pk in enumerate(self.packets):
                    r = pygame.Rect(180 + i*86, 8, 80, 72)
                    if r.collidepoint(ev.pos):
                        self.select_packet(i)
                        return
                # shovel button
                sx = 180 + len(self.packets)*86 + 8
                if pygame.Rect(sx, 8, 60, 72).collidepoint(ev.pos):
                    self.shovel = not self.shovel
                    if self.shovel:
                        self.selected_packet = -1
                    return
                # sun pickup
                if self.click_sun(ev.pos):
                    return
                # plant / shovel
                cell = which_cell(ev.pos)
                if self.shovel:
                    self.try_shovel(cell)
                else:
                    self.try_plant(cell)
            elif ev.button == 3:
                self.selected_packet = -1
                self.shovel = False

    # ---- main loop ---- #
    def run(self):
        while True:
            dt = self.clock.tick(FPS) / 1000.0
            for ev in pygame.event.get():
                self.handle(ev)
            self.update(dt)

            self.screen.fill(C_BG)
            if self.state == "TITLE":
                self.draw_title(self.screen)
            elif self.state == "DAVE":
                self.draw_dave(self.screen)
            elif self.state == "PLAY":
                self.draw_lawn(self.screen)
                self.scene.Draw(self.screen)
                # ghost preview
                if self.selected_packet >= 0:
                    cell = which_cell(pygame.mouse.get_pos())
                    if cell:
                        c, r = cell
                        s = pygame.Surface((CELL_W, CELL_H), pygame.SRCALPHA)
                        s.fill((77, 166, 255, 60))
                        self.screen.blit(s, (LAWN_X + c*CELL_W, LAWN_Y + r*CELL_H))
                # shovel cursor hint
                if self.shovel:
                    cell = which_cell(pygame.mouse.get_pos())
                    if cell:
                        c, r = cell
                        s = pygame.Surface((CELL_W, CELL_H), pygame.SRCALPHA)
                        s.fill((255, 80, 80, 70))
                        self.screen.blit(s, (LAWN_X + c*CELL_W, LAWN_Y + r*CELL_H))
                self.draw_ui(self.screen)
                self.draw_announce(self.screen)
                if self.paused:
                    self.draw_overlay(self.screen, "PAUSED", C_BLUE)
            elif self.state == "WIN":
                self.draw_lawn(self.screen)
                self.scene.Draw(self.screen)
                self.draw_ui(self.screen)
                self.draw_overlay(self.screen, "YOU SAVED THE BRAINS!", C_BLUE)
            elif self.state == "LOSE":
                self.draw_lawn(self.screen)
                self.scene.Draw(self.screen)
                self.draw_ui(self.screen)
                self.draw_overlay(self.screen, "THE ZOMBIES ATE YOUR BRAINS", C_RED)

            pygame.display.flip()


# ============================================================================ #
#  ENTRY                                                                       #
# ============================================================================ #

def main():
    try:
        Game().run()
    except SystemExit:
        raise
    except Exception as e:
        import traceback; traceback.print_exc()
        try:
            pygame.quit()
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
