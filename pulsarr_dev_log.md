# Pulsarr — Development Log

---

## 📋 Backlog / Planned Features
*Logged: 2026-07-02 — Not started, pending implementation*

---

### 1. Project Rename
- [ ] Update project name from current name → **Pulsarr**

---

### 2. Tracker Scoring System
- [ ] Prefer **UDP over HTTP** trackers
- [ ] Measure and factor in **latency/ping**
- [ ] Maintain **historical data per tracker** (performance over time)
- [ ] Use **AI evaluation** of historical data to assess stability & reliability
- [ ] Assign each tracker a **composite score** based on the above

---

### 3. Config — Tracker Import Limit
- [ ] Add a setting under Config to **cap the number of trackers imported** into the client

---

### 4. Rules Engine (future evolution of import limit)
- [ ] Build a configurable rules system for tracker filtering/selection
- **Example rule logic:**
  - Max import: `20` trackers
  - Prefer UDP over HTTP
  - Ping must be `< 199ms`
  - OR score above a user-defined threshold
- [ ] Rules should be user-configurable via UI

---

### 5. URL Resolution & Deduplication *(later)*
- [ ] Resolve trackers that are URLs (hostname lookup)
- [ ] Check for **duplicate trackers** after resolution
- [ ] **Discard duplicates before ping** to avoid wasted checks

---

### 6. Stalled Torrent Rules *(later)*
- [ ] Separate rule set specifically for **stalled torrents**
- [ ] If a torrent stalls mid-run, apply an expanded tracker rule
- **Example:**
  - Default import: Top **20** trackers
  - Stalled torrent fallback: Top **100** trackers
- [ ] Rule should trigger automatically on stall detection

---

*No implementation has begun. This log is for planning/tracking purposes only.*
