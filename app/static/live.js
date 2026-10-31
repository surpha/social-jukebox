// Shared client for the guest page and the host master view.
(function () {
    const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
    const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ESC[c]);

    function formatTime(ms) {
        if (!ms || ms < 0) return '0:00';
        const s = Math.floor(ms / 1000);
        return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
    }

    let toastTimer;
    function toast(msg) {
        const el = document.getElementById('toast');
        if (!el) return;
        document.getElementById('toastText').textContent = msg;
        el.classList.remove('hidden');
        clearTimeout(toastTimer);
        toastTimer = setTimeout(() => el.classList.add('hidden'), 2200);
    }

    const PLUS = '<svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M12 4v16m8-8H4"/></svg>';

    function trackRow(t, { confirm = false } = {}) {
        const id = esc(t.track_id);
        const attrs = confirm ? `data-confirm="${id}" role="button"` : '';
        const action = confirm
            ? ''
            : `<button data-add="${id}" aria-label="Add to queue" class="w-8 h-8 rounded-full bg-sp-green text-sp-black flex items-center justify-center flex-shrink-0 hover:scale-110 active:scale-95 transition-transform shadow-sm">${PLUS}</button>`;
        return `<div ${attrs} class="flex items-center gap-3 p-2.5 rounded-lg hover:bg-sp-card/80 transition-colors ${confirm ? 'cursor-pointer' : ''}">
            <img src="${esc(t.album_art)}" alt="" loading="lazy" decoding="async" class="w-10 h-10 rounded object-cover flex-shrink-0">
            <div class="flex-1 min-w-0">
                <p class="text-sm font-medium truncate">${esc(t.name)}</p>
                <p class="text-xs text-sp-light truncate">${esc(t.artist)}</p>
            </div>${action}</div>`;
    }

    function renderTrackList(section, list, tracks, known) {
        tracks = tracks || [];
        tracks.forEach((t) => known.set(t.track_id, t));
        const fp = tracks.map((t) => t.track_id).join('|');
        if (list.dataset.fp === fp) return;
        list.dataset.fp = fp;
        section.classList.toggle('hidden', tracks.length === 0);
        list.innerHTML = tracks.map((t) => trackRow(t)).join('');
    }

    const BTN = 'flex items-center px-2.5 py-1.5 rounded-full border text-sm font-semibold transition-colors';
    const OFF = 'border-white/10 text-sp-light hover:bg-white/5';
    const UP_ON = 'bg-sp-green/20 border-sp-green/50 text-sp-green';
    const DOWN_ON = 'bg-red-500/20 border-red-500/50 text-red-400';
    const UP_ICON = '<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M5 15l7-7 7 7"/></svg>';
    const DOWN_ICON = '<svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M19 9l-7 7-7-7"/></svg>';

    // Keyed list: rows are created once and patched in place, then slid to their new position.
    class QueueView {
        constructor(list, { myVote, onVote, showDownvote = true }) {
            this.list = list;
            this.rows = new Map();
            this.myVote = myVote;
            this.showDownvote = showDownvote;
            list.addEventListener('click', (e) => {
                const btn = e.target.closest('button[data-vote]');
                if (btn) onVote(btn.closest('[data-track]').dataset.track, btn.dataset.vote);
            });
        }

        _create(item) {
            const el = document.createElement('div');
            el.dataset.track = item.track_id;
            el.className = 'flex items-center gap-3 p-3 rounded-xl bg-sp-dark border border-white/5 fade-in';
            el.innerHTML = `
                <span data-rank class="text-xs text-sp-light/50 font-bold w-5 text-center flex-shrink-0"></span>
                <img src="${esc(item.album_art)}" alt="" loading="lazy" decoding="async" class="w-11 h-11 rounded object-cover flex-shrink-0 shadow">
                <div class="flex-1 min-w-0">
                    <p class="text-sm font-medium truncate">${esc(item.name)}</p>
                    <p class="text-xs text-sp-light truncate">${esc(item.artist)}</p>
                </div>
                <div class="flex items-center gap-1 flex-shrink-0">
                    <button data-vote="up" aria-label="Upvote">${UP_ICON}</button>
                    <span data-count class="text-sm font-semibold text-sp-light min-w-[20px] text-center"></span>
                    ${this.showDownvote ? `<button data-vote="down" aria-label="Downvote">${DOWN_ICON}</button>` : ''}
                </div>`;
            el._rank = el.querySelector('[data-rank]');
            el._count = el.querySelector('[data-count]');
            el._up = el.querySelector('[data-vote="up"]');
            el._down = el.querySelector('[data-vote="down"]');
            return el;
        }

        _patch(el, item, index) {
            const rank = String(index + 1);
            if (el._rank.textContent !== rank) el._rank.textContent = rank;
            const count = String(item.vote_count);
            if (el._count.textContent !== count) el._count.textContent = count;
            const vote = this.myVote(item.track_id);
            const up = `${BTN} ${vote === 'up' ? UP_ON : OFF}`;
            if (el._up.className !== up) el._up.className = up;
            if (el._down) {
                const down = `${BTN} ${vote === 'down' ? DOWN_ON : OFF}`;
                if (el._down.className !== down) el._down.className = down;
            }
        }

        render(items) {
            const before = new Map();
            this.rows.forEach((el, id) => before.set(id, el.getBoundingClientRect().top));
            const seen = new Set();
            items.forEach((item, i) => {
                let el = this.rows.get(item.track_id);
                if (!el) {
                    el = this._create(item);
                    this.rows.set(item.track_id, el);
                }
                this._patch(el, item, i);
                seen.add(item.track_id);
                if (this.list.children[i] !== el) this.list.insertBefore(el, this.list.children[i] || null);
            });
            this.rows.forEach((el, id) => {
                if (!seen.has(id)) { el.remove(); this.rows.delete(id); }
            });
            this.rows.forEach((el, id) => {
                const top = before.get(id);
                if (top === undefined) return;
                const dy = top - el.getBoundingClientRect().top;
                if (Math.abs(dy) > 1 && el.animate) {
                    el.animate([{ transform: `translateY(${dy}px)` }, { transform: 'none' }], { duration: 250, easing: 'ease-out' });
                }
            });
        }
    }

    // Progress is interpolated locally; the server only pushes when the track, play state, or position jumps.
    class NowPlaying {
        constructor(els) {
            this.els = els;
            this.np = null;
            this.anchor = 0;
            this.trackId = null;
            setInterval(() => this._tick(), 1000);
        }

        update(np) {
            const e = this.els;
            this.np = np;
            this.anchor = performance.now();
            e.eq?.classList.toggle('paused', !(np && np.is_playing));
            const id = np ? np.track_id : null;
            if (id !== this.trackId) {
                this.trackId = id;
                if (np) {
                    if (np.album_art) { e.img.src = np.album_art; e.img.classList.remove('hidden'); e.placeholder.classList.add('hidden'); }
                    else { e.img.classList.add('hidden'); e.placeholder.classList.remove('hidden'); }
                    e.title.textContent = np.name;
                    e.title.classList.replace('text-sp-light', 'text-white');
                    e.artist.textContent = np.artist;
                    e.duration.textContent = formatTime(np.duration_ms);
                } else {
                    e.img.classList.add('hidden'); e.placeholder.classList.remove('hidden');
                    e.title.textContent = 'Nothing playing';
                    e.title.classList.replace('text-white', 'text-sp-light');
                    e.artist.textContent = 'Waiting for music...';
                    e.duration.textContent = '0:00';
                }
            }
            this._tick();
        }

        progress() {
            const np = this.np;
            if (!np) return 0;
            const p = np.progress_ms + (np.is_playing ? performance.now() - this.anchor : 0);
            return Math.min(p, np.duration_ms || p);
        }

        _tick() {
            const np = this.np;
            const p = this.progress();
            this.els.bar.style.width = np && np.duration_ms ? `${(p / np.duration_ms) * 100}%` : '0%';
            this.els.elapsed.textContent = formatTime(p);
        }
    }

    function connect(url, onData, onStatus) {
        let es = null;
        let retryTimer = null;
        let closed = false;
        const open = () => {
            if (closed) return;
            if (es) es.close();
            es = new EventSource(url);
            es.onopen = () => onStatus && onStatus(true);
            es.onmessage = (e) => {
                try { onData(JSON.parse(e.data)); } catch (err) { console.error('Bad event', err); }
            };
            es.onerror = () => {
                onStatus && onStatus(false);
                if (es.readyState === EventSource.CLOSED) {
                    clearTimeout(retryTimer);
                    retryTimer = setTimeout(open, 3000);
                }
            };
        };
        const onVisible = () => {
            if (!document.hidden && (!es || es.readyState === EventSource.CLOSED)) open();
        };
        document.addEventListener('visibilitychange', onVisible);
        open();
        return {
            close: () => {
                closed = true;
                clearTimeout(retryTimer);
                document.removeEventListener('visibilitychange', onVisible);
                if (es) es.close();
                es = null;
            },
        };
    }

    window.Live = { esc, formatTime, toast, trackRow, renderTrackList, QueueView, NowPlaying, connect };
})();
