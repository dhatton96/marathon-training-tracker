# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Marathon Training Tracker — a single-file web app (`index.html`) for tracking an 18-week Blackpool Marathon training plan. No build tools or dependencies; just open in a browser.

## Architecture

Everything lives in `index.html`: HTML structure, CSS (in `<style>`), and JavaScript (in `<script>`).

**Key JS sections:**
- `PLAN` array: 18-week training schedule with daily workouts per week object (`{w, phase, km, days:{mon..sun}}`)
- `PACES` array: target pace zones (Threshold, MP, Interval, Repetition, Steady)
- State management: `loadState()`/`saveState()` using `localStorage` key `marathon-tracker-v1`
- `render()`: single function rebuilds the entire UI into `#app` container
- Actions: `navWeek()`, `toggleCheck()`, `saveLog()`

**State shape in localStorage:**
```json
{ "checked": { "w8-tue": true, ... }, "logs": { "w8-tue": { "distance": "18", "pace": "3:22", "notes": "" }, ... } }
```

**Design system:** Nike-style aesthetic using CSS custom properties — `--bg` (black), `--text` (white), `--accent` (#CDFF00 volt green). All styling uses these variables.

## Training Plan Details

- Weeks 1-7 are auto-completed on first load (user started at week 8)
- Progress bar is workout-based (checked workouts / total non-rest workouts across all 18 weeks)
- Pace zones: T 3:20-3:24, MP 3:34, I 3:05-3:10, R 3:55-4:00, Steady 4:00 (all per km)
- Special events: Week 16 Sun = Milan Marathon Trial, Week 18 Sun = Blackpool Marathon
