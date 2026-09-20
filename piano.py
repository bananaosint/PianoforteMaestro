"""
PianoforteMaestro - a falling-note MIDI piano for Windows.

Notes fall vertically down onto an on-screen keyboard during playback, and
rise up off the keys as you play live. MIDI hardware is used when present;
the computer keyboard and the Windows GS Wavetable synth are the fallbacks.
"""

import os
import sys
import glob
import queue
import time

import pygame
import rtmidi
import mido

# ---------------------------------------------------------------- constants

WIDTH, HEIGHT = 1280, 760
KEY_H = 132                       # on-screen keyboard height
HUD_H = 80                        # top status panel
KEY_TOP = HEIGHT - KEY_H          # y of the top edge of the white keys
FALL_H = KEY_TOP - HUD_H          # height of the falling-note area

LOW_NOTE, HIGH_NOTE = 21, 108     # A0 .. C8
N_WHITE = 52

BLACK_PCS = {1, 3, 6, 8, 10}
WHITE_W = WIDTH / N_WHITE
BLACK_W = max(6, int(WHITE_W * 0.58))
BLACK_H = int(KEY_H * 0.62)

# When frozen by PyInstaller, __file__ points inside a temp extraction dir that
# is deleted on exit, so recordings must be written next to the .exe instead.
if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
SONG_DIR = os.path.join(APP_DIR, "songs")

DEFAULT_FALL_SECONDS = 3.0        # time a note takes to fall the full area

BG            = (14, 15, 22)
PANEL         = (22, 24, 34)
GRID          = (30, 33, 46)
WHITE_KEY     = (242, 243, 247)
WHITE_KEY_LIT = (120, 200, 255)
BLACK_KEY     = (26, 28, 38)
BLACK_KEY_LIT = (60, 140, 210)
TEXT          = (208, 214, 228)
DIM           = (120, 128, 148)
ACCENT        = (94, 205, 255)
REC_COLOR     = (255, 82, 96)

NOTE_WHITE    = (94, 205, 255)
NOTE_BLACK    = (150, 130, 255)
NOTE_LIVE_W   = (110, 240, 170)
NOTE_LIVE_B   = (70, 200, 160)

# ------------------------------------------------------------------ geometry


def _build_key_table():
    """Precompute (x, w, is_black) for every note once.

    Shared by the keyboard renderer, the falling-note renderer and mouse
    hit-testing, so those three can never disagree about where a key is.
    """
    table = {}
    white_index = 0
    white_edges = {}                              # note -> (x0, x1)
    for n in range(LOW_NOTE, HIGH_NOTE + 1):
        if n % 12 not in BLACK_PCS:
            x0 = int(round(white_index * WHITE_W))
            x1 = int(round((white_index + 1) * WHITE_W))
            table[n] = (x0, x1 - x0, False)
            white_edges[n] = (x0, x1)
            white_index += 1
    for n in range(LOW_NOTE, HIGH_NOTE + 1):
        if n % 12 in BLACK_PCS:
            lower = n - 1                         # white key just below
            if lower in white_edges:
                boundary = white_edges[lower][1]
            else:
                boundary = white_edges.get(n + 1, (0, 0))[0]
            table[n] = (boundary - BLACK_W // 2, BLACK_W, True)
    return table


KEY_TABLE = _build_key_table()


def key_rect(note):
    """-> (x, width, is_black) for a midi note number, or None if out of range."""
    return KEY_TABLE.get(note)


def note_at_pos(pos):
    """Mouse hit-test. Black keys are checked first because they overlay."""
    x, y = pos
    if y < KEY_TOP:
        return None
    for want_black in (True, False):
        for n, (kx, kw, is_black) in KEY_TABLE.items():
            if is_black != want_black:
                continue
            h = BLACK_H if is_black else KEY_H
            if kx <= x < kx + kw and KEY_TOP <= y < KEY_TOP + h:
                return n
    return None


def note_y(event_time, now, pps, rising):
    """Vertical position of a moment within a note.

    Playback (rising=False): future events sit above the keys and descend,
    reaching KEY_TOP exactly when event_time == now.
    Live (rising=True): the moment is pinned at the keys as it happens and
    travels upward afterwards. Same equation, sign flipped.
    """
    d = (now - event_time) if rising else (event_time - now)
    return KEY_TOP - d * pps


# --------------------------------------------------------------- note model


class Note:
    __slots__ = ("note", "velocity", "start", "end")

    def __init__(self, note, velocity, start, end=None):
        self.note = note
        self.velocity = velocity
        self.start = start
        self.end = end


# ------------------------------------------------------------------- midi io


class MidiIO:
    """Wraps rtmidi.

    Input arrives on rtmidi's own thread and is handed to the main loop
    through a queue - nothing else is touched from that thread.
    """

    def __init__(self):
        self.inq = queue.Queue()
        self._in = None
        self._out = None
        self.in_ports = []
        self.out_ports = []
        self.in_index = -1
        self.out_index = -1
        self.error = ""
        self.rescan()

    def rescan(self):
        try:
            self.in_ports = rtmidi.MidiIn().get_ports()
        except Exception as e:
            self.in_ports, self.error = [], str(e)
        try:
            self.out_ports = rtmidi.MidiOut().get_ports()
        except Exception as e:
            self.out_ports, self.error = [], str(e)
        # Auto-connect: first input; for output prefer real hardware over the
        # Windows software synth, falling back to whatever exists.
        if self._in is None and self.in_ports:
            self.open_in(0)
        if self._out is None and self.out_ports:
            pick = 0
            for i, p in enumerate(self.out_ports):
                if "wavetable" not in p.lower():
                    pick = i
                    break
            self.open_out(pick)

    def _cb(self, event, _data):
        message, _delta = event
        self.inq.put((time.perf_counter(), message))

    def open_in(self, index):
        self.close_in()
        if not (0 <= index < len(self.in_ports)):
            return
        try:
            m = rtmidi.MidiIn()
            m.open_port(index)
            m.set_callback(self._cb)
            self._in, self.in_index, self.error = m, index, ""
        except Exception as e:
            # Windows MIDI ports are exclusive; another app may hold this one.
            self.error = "input '%s': %s" % (self.in_ports[index], e)

    def open_out(self, index):
        self.close_out()
        if not (0 <= index < len(self.out_ports)):
            return
        try:
            m = rtmidi.MidiOut()
            m.open_port(index)
            self._out, self.out_index, self.error = m, index, ""
        except Exception as e:
            self.error = "output '%s': %s" % (self.out_ports[index], e)

    def close_in(self):
        if self._in is not None:
            try:
                self._in.cancel_callback()
                self._in.close_port()
            except Exception:
                pass
        self._in, self.in_index = None, -1

    def close_out(self):
        if self._out is not None:
            try:
                self.panic()
                self._out.close_port()
            except Exception:
                pass
        self._out, self.out_index = None, -1

    def cycle_in(self):
        if self.in_ports:
            self.open_in((self.in_index + 1) % len(self.in_ports))

    def cycle_out(self):
        if self.out_ports:
            self.open_out((self.out_index + 1) % len(self.out_ports))

    def send(self, msg):
        if self._out is not None:
            try:
                self._out.send_message(msg)
            except Exception:
                pass

    def note_on(self, note, vel):
        self.send([0x90, note & 0x7F, vel & 0x7F])

    def note_off(self, note):
        self.send([0x80, note & 0x7F, 0])

    def panic(self):
        for ch in range(16):
            self.send([0xB0 | ch, 123, 0])

    def in_name(self):
        return self.in_ports[self.in_index] if self.in_index >= 0 else "none"

    def out_name(self):
        return self.out_ports[self.out_index] if self.out_index >= 0 else "none"


# --------------------------------------------------------------- midi files


def save_midi(notes, path, tempo=500000, tpb=480):
    """Notes hold absolute seconds; they become ticks only here."""
    mid = mido.MidiFile(ticks_per_beat=tpb)
    track = mido.MidiTrack()
    mid.tracks.append(track)
    track.append(mido.MetaMessage("set_tempo", tempo=tempo, time=0))

    events = []
    for n in notes:
        end = n.end if n.end is not None else n.start + 0.1
        events.append((n.start, 1, n.note, max(1, n.velocity)))
        events.append((end, 0, n.note, 0))
    events.sort(key=lambda e: (e[0], e[1]))        # note-offs before note-ons

    prev_tick = 0
    for t, is_on, note, vel in events:
        tick = int(round(mido.second2tick(t, tpb, tempo)))
        delta = max(0, tick - prev_tick)
        prev_tick = tick
        track.append(mido.Message("note_on" if is_on else "note_off",
                                  note=note, velocity=vel, time=delta))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mid.save(path)


def load_midi(path):
    """-> list[Note] in absolute seconds, honouring tempo changes."""
    mid = mido.MidiFile(path)
    tempo = 500000
    t = 0.0
    open_notes = {}
    notes = []
    for msg in mido.merge_tracks(mid.tracks):
        t += mido.tick2second(msg.time, mid.ticks_per_beat, tempo)
        if msg.type == "set_tempo":
            tempo = msg.tempo
        elif msg.type == "note_on" and msg.velocity > 0:
            open_notes.setdefault(msg.note, []).append(
                Note(msg.note, msg.velocity, t))
        elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
            stack = open_notes.get(msg.note)
            if stack:
                n = stack.pop(0)
                n.end = t
                notes.append(n)
    for stack in open_notes.values():              # never leave one unterminated
        for n in stack:
            n.end = t
            notes.append(n)
    notes.sort(key=lambda n: n.start)
    return notes


# ------------------------------------------------------- computer keyboard


def _kc(ch):
    special = {",": pygame.K_COMMA, ".": pygame.K_PERIOD,
               ";": pygame.K_SEMICOLON, "/": pygame.K_SLASH}
    return special.get(ch, getattr(pygame, "K_" + ch, None))


def _build_computer_map():
    m = {}
    for row, base in (("zsxdcvgbhnjm,l.;/", 0), ("q2w3er5t6y7ui9o0p", 12)):
        for i, ch in enumerate(row):
            k = _kc(ch)
            if k is not None:
                m[k] = base + i
    return m


COMPUTER_MAP = _build_computer_map()


# ------------------------------------------------------------------- the app


class App:
    def __init__(self):
        pygame.init()
        pygame.display.set_caption("PianoforteMaestro")
        self.screen = pygame.display.set_mode((WIDTH, HEIGHT))
        self.clock = pygame.time.Clock()
        self.font = pygame.font.SysFont("consolas", 16)
        self.big = pygame.font.SysFont("consolas", 22, bold=True)

        self.midi = MidiIO()
        self.fall_seconds = DEFAULT_FALL_SECONDS

        self.live_notes = []          # played notes, absolute clock, rising
        self.active_live = {}
        self.song = []                # recorded/loaded notes, seconds from 0
        self.active_rec = {}
        self.held = set()             # every note currently sounding (display)
        self.play_held = set()        # the subset playback is responsible for

        self.recording = False
        self.record_start = 0.0
        self.playing = False
        self.play_start = 0.0
        self.play_events = []
        self.play_index = 0
        self.song_time = 0.0

        self.octave = 60              # computer-keyboard base note (C4)
        self.mouse_note = None
        self.song_files = []
        self.song_index = -1
        self.status = "ready"
        self.refresh_songs()

    @property
    def pps(self):
        """Pixels per second - derived, so geometry and timing stay in sync."""
        return FALL_H / self.fall_seconds

    def refresh_songs(self):
        self.song_files = sorted(glob.glob(os.path.join(SONG_DIR, "*.mid")))

    # -------------------------------------------------------------- sounding

    def start_note(self, note, vel, now):
        # guard on active_live, not held, so you can play a pitch that playback
        # happens to be sounding at the same moment
        if note in self.active_live or not (LOW_NOTE <= note <= HIGH_NOTE):
            return
        self.held.add(note)
        self.midi.note_on(note, vel)
        n = Note(note, vel, now)
        self.live_notes.append(n)
        self.active_live[note] = n
        if self.recording:
            r = Note(note, vel, now - self.record_start)
            self.song.append(r)
            self.active_rec[note] = r

    def stop_note(self, note, now):
        # drive off active_live so a note always gets closed, even if playback
        # has meanwhile removed this pitch from `held`
        n = self.active_live.pop(note, None)
        if n is None and note not in self.held:
            return
        if n is not None:
            n.end = now
        if note not in self.play_held:          # leave playback's note sounding
            self.held.discard(note)
            self.midi.note_off(note)
        r = self.active_rec.pop(note, None)
        if r is not None:
            r.end = now - self.record_start

    def all_off(self, now):
        self.play_held.clear()
        for note in list(self.held):
            self.stop_note(note, now)
        self.held.clear()
        self.midi.panic()

    # ------------------------------------------------------------- transport

    def toggle_record(self, now):
        if self.playing:
            self.stop_playback()
        if self.recording:
            self.recording = False
            # close every note still open, else they become infinite rectangles
            for r in self.active_rec.values():
                r.end = now - self.record_start
            self.active_rec.clear()
            self.song.sort(key=lambda n: n.start)
            self.status = "recorded %d notes" % len(self.song)
        else:
            self.song = []
            self.active_rec.clear()
            self.recording = True
            self.record_start = now
            self.status = "recording"

    def start_playback(self, now):
        if not self.song:
            self.status = "nothing to play"
            return
        events = []
        for n in self.song:
            end = n.end if n.end is not None else n.start + 0.1
            events.append((n.start, 1, n.note, max(1, n.velocity)))
            events.append((end, 0, n.note, 0))
        events.sort(key=lambda e: (e[0], e[1]))
        self.play_events = events
        self.play_index = 0
        self.playing = True
        # lead-in of one full fall, so the opening notes visibly drop in
        self.play_start = now + self.fall_seconds
        self.song_time = now - self.play_start
        self.status = "playing"

    def stop_playback(self):
        self.playing = False
        self.play_events = []
        self.play_index = 0
        # only silence what playback itself started; anything the player is
        # still holding stays down, and stays tracked in active_live
        for note in list(self.play_held):
            self._release_played(note)
        self.play_held.clear()
        self.status = "stopped"

    def _release_played(self, note):
        self.play_held.discard(note)
        if note in self.active_live:     # the player is holding it too
            return
        self.held.discard(note)
        self.midi.note_off(note)

    def pump_playback(self, now):
        self.song_time = now - self.play_start
        while self.play_index < len(self.play_events):
            t, is_on, note, vel = self.play_events[self.play_index]
            if t > self.song_time:
                break
            if is_on:
                self.play_held.add(note)
                self.held.add(note)
                self.midi.note_on(note, vel)
            else:
                self._release_played(note)
            self.play_index += 1
        if self.play_index >= len(self.play_events):
            tail = self.play_events[-1][0] if self.play_events else 0.0
            if self.song_time > tail + 0.25:
                self.stop_playback()

    # ----------------------------------------------------------------- files

    def save_song(self):
        if not self.song:
            self.status = "nothing to save"
            return
        name = time.strftime("song-%Y%m%d-%H%M%S.mid")
        path = os.path.join(SONG_DIR, name)
        try:
            save_midi(self.song, path)
            self.refresh_songs()
            self.song_index = self.song_files.index(path)
            self.status = "saved " + name
        except Exception as e:
            self.status = "save failed: %s" % e

    def load_next_song(self):
        self.refresh_songs()
        if not self.song_files:
            self.status = "songs/ is empty"
            return
        if self.playing:
            self.stop_playback()
        self.song_index = (self.song_index + 1) % len(self.song_files)
        path = self.song_files[self.song_index]
        try:
            self.song = load_midi(path)
            self.status = "loaded %s (%d notes)" % (
                os.path.basename(path), len(self.song))
        except Exception as e:
            self.status = "load failed: %s" % e

    # ----------------------------------------------------------------- input

    def drain_midi(self, now):
        """Consume everything the rtmidi thread queued, on the main thread."""
        while True:
            try:
                _ts, msg = self.midi.inq.get_nowait()
            except queue.Empty:
                return
            if len(msg) < 3:
                continue
            status, note, vel = msg[0] & 0xF0, msg[1], msg[2]
            if status == 0x90 and vel > 0:
                self.start_note(note, vel, now)
            elif status == 0x80 or (status == 0x90 and vel == 0):
                self.stop_note(note, now)

    def handle_key(self, event, now):
        k = event.key
        if event.type == pygame.KEYDOWN:
            if k == pygame.K_ESCAPE:
                return False
            elif k == pygame.K_TAB:
                self.toggle_record(now)
            elif k == pygame.K_SPACE:
                if self.playing:
                    self.stop_playback()
                else:
                    self.start_playback(now)
            elif k == pygame.K_BACKSPACE:
                self.song = []
                self.active_rec.clear()
                self.status = "cleared"
            elif k == pygame.K_F1:
                self.midi.cycle_in()
            elif k == pygame.K_F2:
                self.midi.cycle_out()
            elif k == pygame.K_F3:
                self.save_song()
            elif k == pygame.K_F4:
                self.load_next_song()
            elif k == pygame.K_F5:
                self.midi.rescan()
                self.status = "rescanned midi ports"
            elif k == pygame.K_LEFT:
                self.octave = max(24, self.octave - 12)
            elif k == pygame.K_RIGHT:
                self.octave = min(HIGH_NOTE - 28, self.octave + 12)
            elif k == pygame.K_UP:
                self.fall_seconds = max(0.8, self.fall_seconds - 0.25)
            elif k == pygame.K_DOWN:
                self.fall_seconds = min(8.0, self.fall_seconds + 0.25)
            elif k in COMPUTER_MAP:
                self.start_note(self.octave + COMPUTER_MAP[k], 80, now)
        else:
            if k in COMPUTER_MAP:
                self.stop_note(self.octave + COMPUTER_MAP[k], now)
        return True

    # --------------------------------------------------------------- drawing

    def draw_falling(self, now):
        """The song descends onto the keys; what you play rises up off them.

        Both passes run during playback, so you can play along and see your
        own notes leaving as the song's notes arrive.
        """
        if self.playing:
            self._draw_pass(self.song, self.song_time, rising=False)
        self._draw_pass(self.live_notes, now, rising=True)

    def _draw_pass(self, notes, clock, rising):
        pps = self.pps
        for n in notes:
            rect = key_rect(n.note)
            if rect is None:
                continue
            x, w, is_black = rect
            end = n.end if n.end is not None else clock
            if rising:
                y_bottom = note_y(end, clock, pps, True)
                y_top = note_y(n.start, clock, pps, True)
            else:
                y_bottom = note_y(n.start, clock, pps, False)
                y_top = note_y(end, clock, pps, False)
            if y_bottom < HUD_H or y_top > KEY_TOP:
                continue
            yt = max(float(HUD_H), y_top)
            yb = min(float(KEY_TOP), y_bottom)
            if yb - yt < 1:
                continue
            col = (NOTE_LIVE_B if is_black else NOTE_LIVE_W) if rising else \
                  (NOTE_BLACK if is_black else NOTE_WHITE)
            if rising:      # fade live notes out as they travel away
                fade = max(0.3, 1.0 - (KEY_TOP - yb) / float(FALL_H))
                col = tuple(int(v * fade) for v in col)
            r = pygame.Rect(int(x) + 1, int(yt), max(2, int(w) - 2),
                            max(2, int(yb - yt)))
            pygame.draw.rect(self.screen, col, r, border_radius=4)
            pygame.draw.rect(self.screen,
                             tuple(min(255, int(v * 1.45)) for v in col),
                             r, width=1, border_radius=4)

    def draw_lanes(self):
        for n in range(LOW_NOTE, HIGH_NOTE + 1):
            if n % 12 != 0:
                continue
            x, _w, _b = KEY_TABLE[n]
            pygame.draw.line(self.screen, GRID, (x, HUD_H + 1), (x, KEY_TOP))

    def draw_keyboard(self):
        # the strike line the notes land on
        pygame.draw.rect(self.screen, (8, 9, 14), (0, KEY_TOP - 5, WIDTH, 5))
        pygame.draw.rect(self.screen, ACCENT, (0, KEY_TOP - 2, WIDTH, 2))
        for n in self.held:                       # glow over each sounding key
            rect = key_rect(n)
            if rect:
                x, w, _b = rect
                pygame.draw.rect(self.screen, (170, 230, 255),
                                 (x, KEY_TOP - 5, w, 5))

        for n in range(LOW_NOTE, HIGH_NOTE + 1):
            x, w, is_black = KEY_TABLE[n]
            if is_black:
                continue
            lit = n in self.held
            pygame.draw.rect(self.screen, WHITE_KEY_LIT if lit else WHITE_KEY,
                             (x, KEY_TOP, w, KEY_H))
            pygame.draw.rect(self.screen, (168, 174, 190),
                             (x, KEY_TOP, w, KEY_H), width=1)
            if n % 12 == 0:
                lbl = self.font.render("C%d" % (n // 12 - 1), True, (146, 152, 168))
                self.screen.blit(lbl, (x + 3, KEY_TOP + KEY_H - 21))
        for n in range(LOW_NOTE, HIGH_NOTE + 1):
            x, w, is_black = KEY_TABLE[n]
            if not is_black:
                continue
            lit = n in self.held
            pygame.draw.rect(self.screen, (6, 7, 11),
                             (x - 1, KEY_TOP, w + 2, BLACK_H + 3), border_radius=3)
            pygame.draw.rect(self.screen, BLACK_KEY_LIT if lit else BLACK_KEY,
                             (x, KEY_TOP, w, BLACK_H), border_radius=3)

    def draw_hud(self, now):
        pygame.draw.rect(self.screen, PANEL, (0, 0, WIDTH, HUD_H))
        pygame.draw.line(self.screen, GRID, (0, HUD_H), (WIDTH, HUD_H))

        if self.recording:
            mode, col = "REC  %6.1fs" % (now - self.record_start), REC_COLOR
            pygame.draw.circle(self.screen, REC_COLOR, (WIDTH - 20, 20), 7)
        elif self.playing:
            mode, col = "PLAY %6.1fs" % max(0.0, self.song_time), ACCENT
        else:
            mode, col = "LIVE", (140, 230, 180)
        self.screen.blit(self.big.render(mode, True, col), (14, 10))

        info = "in: %-26s out: %-26s" % (self.midi.in_name()[:26],
                                         self.midi.out_name()[:26])
        self.screen.blit(self.font.render(info, True, TEXT), (200, 10))

        line2 = "song:%3d notes   fall:%.2fs   kbd octave:C%d   %s" % (
            len(self.song), self.fall_seconds, self.octave // 12 - 1, self.status)
        self.screen.blit(self.font.render(line2, True, DIM), (200, 31))

        # A warning about the MIDI connection never hides the shortcut line.
        if self.midi.error:
            warn, wcol = "midi: " + self.midi.error[:52], REC_COLOR
        elif not self.midi.in_ports:
            warn, wcol = ("no MIDI input - using computer keys z..m / q..p "
                          "(F5 rescan)"), (210, 176, 90)
        else:
            warn = None
        if warn:
            surf = self.font.render(warn, True, wcol)
            self.screen.blit(surf, (WIDTH - surf.get_width() - 34, 31))

        hints = ("TAB rec   SPACE play   F3 save   F4 load   BKSP clear   "
                 "F1/F2 midi in/out   F5 rescan   arrows octave/speed   ESC quit")
        self.screen.blit(self.font.render(hints, True, (104, 112, 134)), (14, 55))

    # ------------------------------------------------------------------ loop

    def prune(self, now):
        limit = FALL_H + 100
        self.live_notes = [n for n in self.live_notes
                           if n.end is None or (now - n.end) * self.pps < limit]

    def run(self):
        running = True
        while running:
            now = time.perf_counter()
            for event in pygame.event.get():
                if event.type == pygame.QUIT:
                    running = False
                elif event.type in (pygame.KEYDOWN, pygame.KEYUP):
                    if not self.handle_key(event, now):
                        running = False
                elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    n = note_at_pos(event.pos)
                    if n is not None:
                        self.mouse_note = n
                        self.start_note(n, 90, now)
                elif event.type == pygame.MOUSEBUTTONUP and event.button == 1:
                    if self.mouse_note is not None:
                        self.stop_note(self.mouse_note, now)
                        self.mouse_note = None

            self.drain_midi(now)
            if self.playing:
                self.pump_playback(now)
            self.prune(now)

            self.screen.fill(BG)
            self.draw_lanes()
            self.draw_falling(now)
            self.draw_keyboard()
            self.draw_hud(now)
            pygame.display.flip()
            self.clock.tick(60)

        self.all_off(time.perf_counter())
        self.midi.close_in()
        self.midi.close_out()
        pygame.quit()


# ------------------------------------------------------------------ selftest


def selftest():
    ok = True

    def check(cond, label):
        nonlocal ok
        print(("  PASS  " if cond else "  FAIL  ") + label)
        if not cond:
            ok = False

    # 1. every key has an on-screen rect; white keys tile exactly
    whites = []
    inside = True
    for n in range(LOW_NOTE, HIGH_NOTE + 1):
        r = key_rect(n)
        if r is None:
            inside = False
            continue
        x, w, is_black = r
        if x < 0 or x + w > WIDTH or w < 2:
            inside = False
        if not is_black:
            whites.append((x, x + w))
    check(inside, "all %d keys have on-screen rects" % (HIGH_NOTE - LOW_NOTE + 1))
    check(len(whites) == N_WHITE,
          "white key count == %d (got %d)" % (N_WHITE, len(whites)))
    whites.sort()
    check(all(whites[i][1] == whites[i + 1][0] for i in range(len(whites) - 1)),
          "white keys tile with no overlap and no gaps")

    straddle = True
    for n in range(LOW_NOTE, HIGH_NOTE + 1):
        x, w, is_black = key_rect(n)
        if not is_black:
            continue
        lower = key_rect(n - 1)
        if lower and not (x < lower[0] + lower[1] < x + w):
            straddle = False
    check(straddle, "black keys centred on the white-key boundary")

    # 2. the lookahead equation
    pps = FALL_H / DEFAULT_FALL_SECONDS
    check(abs(note_y(10.0, 10.0, pps, False) - KEY_TOP) < 1e-9,
          "falling note bottom lands on KEY_TOP exactly when start == now")
    check(abs(note_y(10.0, 10.0 - DEFAULT_FALL_SECONDS, pps, False) - HUD_H) < 1e-6,
          "a note one fall-time away enters at the top of the fall area")
    check(note_y(10.0, 11.0, pps, True) < KEY_TOP,
          "live notes rise above the keys after being played")
    check(abs(note_y(10.0, 10.0, pps, True) - KEY_TOP) < 1e-9,
          "live note is pinned at the keys at the moment it sounds")

    # 3. midi round-trip through a real file, written to the location the app
    #    actually saves to. Frozen by PyInstaller that must be beside the .exe,
    #    not the temp extraction dir, or recordings vanish when the app exits.
    print("  ....  frozen=%s  songs -> %s" % (bool(getattr(sys, "frozen", False)),
                                              SONG_DIR))
    src = [Note(60, 100, 0.0, 0.5), Note(64, 90, 0.5, 1.0), Note(67, 80, 0.5, 1.25)]
    path = os.path.join(SONG_DIR, "_selftest.mid")
    save_midi(src, path)
    check(os.path.isfile(path), "recordings are writable at %s" % SONG_DIR)
    back = load_midi(path)
    os.remove(path)
    check(len(back) == len(src),
          "round-trip keeps %d notes (got %d)" % (len(src), len(back)))
    worst = 0.0
    for a, b in zip(sorted(src, key=lambda n: (n.start, n.note)),
                    sorted(back, key=lambda n: (n.start, n.note))):
        worst = max(worst, abs(a.start - b.start), abs(a.end - b.end))
        if a.note != b.note:
            worst = 999.0
    check(worst < 0.01, "round-trip timing/pitch accurate (worst delta %.4fs)" % worst)

    # 4. computer-keyboard fallback exists
    check(len(COMPUTER_MAP) >= 28,
          "computer keyboard maps %d keys" % len(COMPUTER_MAP))

    # 5. playing along DURING playback must not strand notes. Live and
    #    playback share the display set `held`, so this is where the two
    #    paths can desync and leave a bar on screen forever.
    os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
    os.environ.setdefault("SDL_AUDIODRIVER", "dummy")
    app = App()
    t0 = time.perf_counter()
    app.song = [Note(60, 100, 0.0, 0.6), Note(64, 100, 0.2, 0.8)]
    app.start_playback(t0)
    lead = app.fall_seconds
    app.pump_playback(t0 + lead + 0.05)
    app.start_note(60, 80, t0 + lead + 0.10)     # same pitch playback is on
    app.start_note(72, 80, t0 + lead + 0.10)
    app.pump_playback(t0 + lead + 0.70)          # playback drops its own 60
    check(60 in app.held, "held pitch survives playback releasing the same note")
    app.stop_playback()
    check(60 in app.held and 72 in app.held,
          "notes the player holds survive stop_playback")
    app.stop_note(60, t0 + lead + 1.0)
    app.stop_note(72, t0 + lead + 1.0)
    check(not app.active_live and app.held == set(),
          "everything closes once the player lets go")
    check(all(n.end is not None for n in app.live_notes),
          "no live note left open (open ones render as infinite bars)")
    app.prune(t0 + lead + 60.0)
    check(app.live_notes == [], "live notes eventually prune away")
    pygame.quit()

    print("\nselftest: " + ("OK" if ok else "FAILED"))
    return 0 if ok else 1


def _fatal(exc):
    """Packaged with --windowed there is no console, so a startup failure would
    otherwise look like the app simply not launching. Show it instead."""
    import traceback
    text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    # The dialog goes first and stands alone: in a --windowed build sys.stderr
    # is None, so writing to it raises and would otherwise kill this handler
    # before the user ever saw the error.
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None, "PianoforteMaestro could not start.\n\n" + text[-1400:],
            "PianoforteMaestro", 0x10)
    except Exception:
        pass
    if sys.stderr is not None:
        sys.stderr.write(text)


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    try:
        App().run()
    except Exception as _e:
        _fatal(_e)
        sys.exit(1)
