/* The roster stays in memory; only controls and selected IDs use session storage. */
(() => {
    const normalize = (value) => String(value ?? '').toLowerCase().normalize('NFKD')
        .replace(/\p{M}/gu, '').replace(/[æœßþðøłđı]/g,
            (letter) => ({æ: 'ae', œ: 'oe', ß: 'ss', þ: 'th', ð: 'd', ø: 'o', ł: 'l', đ: 'd', ı: 'i'}[letter]));
    function matches(rider, filters) {
        const query = normalize(filters.q).trim();
        if (query && !normalize(rider.search_name).includes(query)
            && query !== String(rider.rider_id)
            && (rider.first_cycling_id == null || query !== String(rider.first_cycling_id))) return false;
        if (filters.team && !normalize(rider.search_team).includes(normalize(filters.team).trim())) return false;
        for (const key of ['country', 'division']) {
            if (filters[key] && String(rider[`${key}_id`]) !== filters[key]) return false;
        }
        if (filters.stat_key) {
            const value = rider[`stat_${filters.stat_key}`];
            if (filters.stat_min && (value == null || value < Number(filters.stat_min))) return false;
            if (filters.stat_max && (value == null || value > Number(filters.stat_max))) return false;
        }
        const flag = filters.identity_flag;
        if (flag === 'missing_fc' && rider.first_cycling_id) return false;
        if (flag === 'override' && !rider.pcs_identity_verified_override) return false;
        if (flag && !['missing_fc', 'override'].includes(flag)
            && (rider.pcs_identity_verified_override || rider.pcs_identity_status !== flag)) return false;
        const days = rider.days_since_last_race;
        const activity = {
            untracked: rider.monitor_id == null,
            awaiting: rider.monitor_id != null && rider.latest_check_at == null,
            checked: rider.latest_check_at != null,
            no_race: rider.monitor_id != null && days == null,
            recent: days != null && days <= 90,
            inactive: days != null && days > 90,
        };
        return !filters.activity_status || activity[filters.activity_status] === true;
    }
    function compare(a, b, key, direction) {
        const first = a[key], second = b[key];
        if (first == null || first === '') return second == null || second === '' ? 0 : 1;
        if (second == null || second === '') return -1;
        return (typeof first === 'number' && typeof second === 'number'
            ? first - second : String(first).localeCompare(String(second))) * direction;
    }
    const snapshotNode = document.getElementById('rider-snapshot');
    if (!snapshotNode) return;
    const config = JSON.parse(document.getElementById('rider-table-config').textContent);
    let snapshot = JSON.parse(snapshotNode.textContent);
    // Release the duplicate JSON string after parsing the page snapshot.
    snapshotNode.remove();
    const table = document.querySelector('.riders-table');
    const filtersForm = document.getElementById('riders-filters-form');
    const selectionForm = document.getElementById('rider-selection-form');
    const viewControl = document.getElementById('rider-view');
    const perPage = document.getElementById('per_page');
    const refresh = document.getElementById('refresh-riders');
    const error = document.getElementById('rider-snapshot-error');
    const filterKeys = ['q', 'country', 'division', 'team', 'stat_key', 'stat_min', 'stat_max', 'identity_flag', 'activity_status'];
    const defaults = Object.fromEntries(filterKeys.map((key) => [key, '']));
    let state = {...defaults, ...config.state, sortKey: '', sortDirection: 1};
    let selected = new Set();
    const limit = Number(selectionForm?.dataset.limit || 0);
    const validViews = [...viewControl.options].map((option) => option.value);
    const validPages = [...perPage.options].map((option) => Number(option.value));
    try {
        const saved = JSON.parse(sessionStorage.getItem(config.storageKey));
        if (saved && typeof saved === 'object') state = {...state, ...saved};
        const savedIds = JSON.parse(sessionStorage.getItem(`${config.storageKey}:selection`));
        if (Array.isArray(savedIds)) selected = new Set(savedIds.map(String).slice(0, limit));
    } catch (_) { /* The page also works without storage. */ }
    if (!validViews.includes(state.view)) state.view = validViews[0];
    if (!validPages.includes(Number(state.per_page))) state.per_page = Number(perPage.value);
    state.page = Math.max(1, Number(state.page) || 1);
    if (!filtersForm.elements.activity_status) state.activity_status = '';
    if (!config.stats.includes(state.stat_key)) state.stat_key = '';
    state.sortDirection = state.sortDirection === -1 ? -1 : 1;
    for (const key of filterKeys) {
        const control = filtersForm.elements[key];
        if (control) {
            control.value = state[key] ?? '';
            state[key] = control.value;
        }
    }
    viewControl.value = state.view;
    perPage.value = state.per_page;
    const escape = (value) => String(value ?? '').replace(/[&<>"']/g,
        (character) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[character]));
    const text = (value) => escape(value == null || value === '' ? '-' : value);
    const link = (url, label, external = false) => `<a href="${escape(url)}"${external ? ' target="_blank" rel="noopener noreferrer"' : ''}>${text(label)}</a>`;
    const columns = () => {
        const base = [
            ['name', 'Name', (r) => link(r.rider_url, r.name || `Rider #${r.rider_id}`)
                + (r.has_pending_attribute_changes ? ` <a href="${escape(r.rider_url)}#rider-attribute-approvals" class="pending-approval" aria-label="Attributes pending approval" title="Attributes pending approval"><span aria-hidden="true">⏳</span></a>` : ''), 'col-rider text-truncate-cell'],
            ['team_name', 'Team', (r) => r.team_url ? link(r.team_url, r.team_name || `Team #${r.team_id}`) : text(r.team_name || '-'), 'col-team text-truncate-cell'],
            ['nation_name', 'Country', (r) => text(r.nation_name || '-'), 'col-country'],
        ];
        const views = {
            stats: config.stats.map((stat) => [`stat_${stat}`, stat.toUpperCase(), (r) => text(r[`stat_${stat}`]), 'col-stat rider-stat stat-value']),
            attributes: [
                ['birth_date_display', 'Birth Date'],
                ['size_cm', 'Height', (r) => r.size_cm == null ? '-' : `${text(r.size_cm)} cm`],
                ['weight_kg', 'Weight', (r) => r.weight_kg == null ? '-' : `${text(r.weight_kg)} kg`],
            ],
            activity: [
                ['latest_race_name', 'Last Race'], ['latest_result_date', 'Race Date'],
                ['days_since_last_race', 'Days Since Race'], ['latest_check_at', 'Last Checked', (r) => text(r.check_display)],
                ['latest_check_status', 'Check Status', (r) => text(r.check_status_display)],
            ],
            identity: [
                ['pcs_slug', 'PCS', (r) => r.pcs_slug && r.pcs_slug !== '0' ? link(`https://www.procyclingstats.com/rider/${encodeURIComponent(r.pcs_slug)}`, r.pcs_slug, true) : '-'],
                ['identity_id', 'Identity ID', (r) => r.mapping_conflict ? 'Mapping conflict' : text(r.identity_id)],
                ['first_cycling_id', 'FirstCycling ID', (r) => r.first_cycling_id ? link(`https://firstcycling.com/rider.php?r=${encodeURIComponent(r.first_cycling_id)}`, r.first_cycling_id, true) : '-'],
                ['birth_date_display', 'DOB'],
                ['pcs_identity_status', 'Flags', (r) => {
                    const status = r.pcs_identity_verified_override ? 'override' : r.pcs_identity_status;
                    const label = config.identityLabels[status] || config.identityLabels.unknown;
                    const flag = (symbol, title) => `<span class="rider-flag" role="img" aria-label="${escape(title)}" title="${escape(title)}" tabindex="0">${symbol}</span>`;
                    return flag(({verified: '✅', review: '⚠️', failed: '❌', unknown: '⚠️', override: '✅'})[status] || '⚠️', label)
                        + (!r.first_cycling_id ? flag('🆔', 'Missing FirstCycling ID') : '');
                }],
            ],
        };
        return [...base, ...views[state.view]];
    };
    function persist() {
        try {
            sessionStorage.setItem(config.storageKey, JSON.stringify(state));
            sessionStorage.setItem(`${config.storageKey}:selection`, JSON.stringify([...selected]));
        } catch (_) { /* Keep working if storage is unavailable. */ }
    }
    function updateSelection() {
        if (!selectionForm) return;
        table.querySelectorAll('.rider-select').forEach((box) => {
            box.checked = selected.has(box.value);
            box.closest('tr').classList.toggle('rider-selected', box.checked);
        });
        document.getElementById('rider-selection-count').textContent = selected.size;
        selectionForm.elements.rider_ids.value = [...selected].join(',');
        document.querySelectorAll('button[form="rider-selection-form"], #clear-rider-selection')
            .forEach((button) => { button.disabled = selected.size === 0; });
        persist();
    }
    function pruneSelection() {
        const eligible = new Set(snapshot.riders.filter((r) => !r.is_deleted && r.rider_id > 0).map((r) => String(r.rider_id)));
        selected = new Set([...selected].filter((id) => eligible.has(id)).slice(0, limit));
    }
    function render() {
        const fields = columns();
        let riders = snapshot.riders.filter((r) => matches(r, state));
        if (state.sortKey) riders.sort((a, b) => compare(a, b, state.sortKey, state.sortDirection));
        const total = riders.length;
        const pages = Math.max(1, Math.ceil(total / state.per_page));
        state.page = Math.min(state.page, pages);
        const start = (state.page - 1) * state.per_page;
        riders = riders.slice(start, start + Number(state.per_page));
        table.classList.toggle('riders-table-attributes', state.view === 'attributes');
        table.querySelector('colgroup').innerHTML = (selectionForm ? '<col class="col-select">' : '')
            + fields.map((field) => `<col class="${field[3]?.split(' ')[0] || ''}">`).join('');
        table.tHead.innerHTML = `<tr>${selectionForm ? '<th scope="col"><span class="visually-hidden">Select</span></th>' : ''}`
            + fields.map(([key, label, _, cls]) => `<th scope="col" class="${cls?.includes('col-stat') ? 'col-stat' : ''}" aria-sort="${state.sortKey === key ? (state.sortDirection === 1 ? 'ascending' : 'descending') : 'none'}"><button type="button" class="btn btn-link p-0 text-reset fw-semibold text-decoration-none" data-sort="${key}">${escape(label)}</button></th>`).join('') + '</tr>';
        table.querySelectorAll('[data-bs-toggle="tooltip"]').forEach((node) => globalThis.bootstrap?.Tooltip.getInstance(node)?.dispose());
        table.tBodies[0].innerHTML = riders.map((rider) => `<tr>${selectionForm ? `<td class="rider-selection-cell"><input class="form-check-input rider-select" type="checkbox" value="${rider.rider_id}" aria-label="Select ${escape(rider.name || `Rider #${rider.rider_id}`)}"${rider.is_deleted || rider.rider_id <= 0 ? ' disabled' : ''}></td>` : ''}`
            + fields.map(([key, _, display, cls]) => {
                const value = rider[key];
                let range = '';
                if (key.startsWith('stat_') && value != null) range = ` range-${value <= 65 ? 'low' : value <= 69 ? 'decent' : value <= 73 ? 'good' : value <= 77 ? 'very-good' : value <= 81 ? 'excellent' : 'elite'}`;
                return `<td class="${cls || ''}${range}${key === 'name' && rider.has_pending_attribute_changes ? ' has-pending-attributes' : ''}">${display ? display(rider) : text(value)}</td>`;
            }).join('') + '</tr>').join('');
        document.getElementById('rider-table-title').textContent = `Riders (${total})`;
        document.getElementById('rider-result-start').textContent = total ? start + 1 : 0;
        document.getElementById('rider-result-end').textContent = Math.min(start + Number(state.per_page), total);
        document.getElementById('rider-result-total').textContent = total;
        document.getElementById('riders-empty').hidden = total !== 0;
        const nav = document.getElementById('rider-pagination');
        nav.hidden = pages <= 1;
        const pageButton = (label, page, disabled = false) => `<li class="page-item${page === state.page && typeof label === 'number' ? ' active' : ''}"><button type="button" class="page-link" data-page="${page}"${disabled ? ' disabled' : ''}${page === state.page && typeof label === 'number' ? ' aria-current="page"' : ''}>${label}</button></li>`;
        let pagination = pageButton('Previous', state.page - 1, state.page === 1);
        let last = 0;
        for (let page = 1; page <= pages; page++) {
            if (page !== 1 && page !== pages && Math.abs(page - state.page) > 2) continue;
            if (page > last + 1) pagination += '<li class="page-item disabled"><span class="page-link">…</span></li>';
            pagination += pageButton(page, page);
            last = page;
        }
        nav.innerHTML = `<ul class="pagination mb-0 flex-wrap">${pagination}${pageButton('Next', state.page + 1, state.page === pages)}</ul>`;
        document.getElementById('rider-snapshot-updated').textContent = `Updated ${new Date(snapshot.updated_at).toLocaleTimeString()}`;
        updateSelection();
        persist();
    }
    function applyFilters() {
        const minimum = filtersForm.elements.stat_min;
        const maximum = filtersForm.elements.stat_max;
        maximum.setCustomValidity(minimum.value && maximum.value && Number(minimum.value) > Number(maximum.value)
            ? 'Maximum must be at least the minimum.' : '');
        if (!filtersForm.reportValidity()) return;
        for (const key of filterKeys) state[key] = filtersForm.elements[key]?.value.trim() || '';
        state.per_page = Number(perPage.value);
        state.page = 1;
        render();
    }
    filtersForm.addEventListener('submit', (event) => { event.preventDefault(); applyFilters(); });
    filtersForm.addEventListener('input', applyFilters);
    filtersForm.addEventListener('change', applyFilters);
    perPage.addEventListener('change', applyFilters);
    viewControl.addEventListener('change', () => { state.view = viewControl.value; render(); });
    function stepView(direction) {
        const index = (validViews.indexOf(state.view) + direction + validViews.length) % validViews.length;
        viewControl.value = validViews[index];
        viewControl.dispatchEvent(new Event('change'));
    }
    document.getElementById('previous-rider-view').addEventListener('click', () => stepView(-1));
    document.getElementById('next-rider-view').addEventListener('click', () => stepView(1));
    document.getElementById('reset-riders-filters').addEventListener('click', () => {
        for (const key of filterKeys) if (filtersForm.elements[key]) filtersForm.elements[key].value = '';
        applyFilters();
    });
    table.tHead.addEventListener('click', (event) => {
        const key = event.target.closest('[data-sort]')?.dataset.sort;
        if (!key) return;
        state.sortDirection = state.sortKey === key ? -state.sortDirection : 1;
        state.sortKey = key;
        render();
    });
    document.getElementById('rider-pagination').addEventListener('click', (event) => {
        const page = event.target.closest('[data-page]');
        if (!page || page.disabled) return;
        state.page = Number(page.dataset.page);
        render();
    });
    if (selectionForm) {
        selectionForm.addEventListener('submit', (event) => {
            if (!selected.size) event.preventDefault();
            persist();
        });
        selectionForm.addEventListener('change', (event) => {
            const box = event.target;
            if (!box.matches('.rider-select') || box.disabled) return;
            if (!box.checked) selected.delete(box.value);
            else if (selected.size < limit) selected.add(box.value);
            updateSelection();
        });
        bindRiderDragSelection(selectionForm, {
            cellSelector: '.rider-selection-cell', boxes: () => [...table.querySelectorAll('.rider-select')],
            maxSelected: limit, getSelection: () => selected,
            setSelection: (ids) => { selected = ids; updateSelection(); },
        });
        document.getElementById('clear-rider-selection').addEventListener('click', () => { selected.clear(); updateSelection(); });
    }
    refresh.addEventListener('click', async () => {
        if (refresh.disabled) return;
        refresh.disabled = true;
        error.hidden = true;
        try {
            const response = await fetch(config.snapshotUrl, {cache: 'no-store', headers: {Accept: 'application/json'}});
            if (!response.ok || response.redirected) throw new Error('Refresh failed');
            const next = await response.json();
            if (next.namespace_id !== config.namespaceId || !Array.isArray(next.riders)) throw new Error('Invalid roster');
            snapshot = next;
            for (const [name, options] of [['country', next.countries], ['division', next.divisions]]) {
                const select = filtersForm.elements[name];
                select.innerHTML = `<option value="">All ${name === 'country' ? 'countries' : 'divisions'}</option>`
                    + options.map((option) => `<option value="${escape(option.value)}">${escape(option.label)}</option>`).join('');
                select.value = state[name];
                if (!select.value) state[name] = '';
            }
            pruneSelection();
            render();
        } catch (_) {
            error.textContent = 'Could not refresh riders. Your current table is still available. Please try again.';
            error.hidden = false;
        } finally { refresh.disabled = false; }
    });
    window.addEventListener('pageshow', (event) => { if (event.persisted) refresh.click(); });
    pruneSelection();
    render();
})();
