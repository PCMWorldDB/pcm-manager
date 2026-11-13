(() => {
    const form = document.getElementById('add-pcs-race-form');
    if (!form) return;
    const button = document.getElementById('get-pcs-race');
    const progress = document.getElementById('pcs-race-progress');
    const spinner = document.getElementById('pcs-race-spinner');
    const outcome = document.getElementById('pcs-race-outcome');
    const message = document.getElementById('pcs-race-message');
    const link = document.getElementById('pcs-race-result-link');
    let pending = false;
    function finish(data) {
        spinner.hidden = true;
        button.disabled = pending = false;
        const success = data.status === 'success';
        outcome.textContent = success ? '✓ ' : '✕ ';
        outcome.className = success ? 'text-success' : 'text-danger';
        message.textContent = success ? 'PCS race fetched.' : data.error || 'Could not fetch the PCS race.';
        link.hidden = !success;
        if (success) link.href = data.race_url;
    }
    async function read(response) {
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'Could not check the PCS race.');
        return data;
    }
    async function poll(url) {
        try {
            const data = await read(await fetch(url, {headers: {Accept: 'application/json'}, cache: 'no-store'}));
            if (data.status === 'success' || data.status === 'failed') finish(data);
            else window.setTimeout(() => poll(url), 1000);
        } catch (error) { finish({error: error.message}); }
    }
    form.addEventListener('submit', async (event) => {
        event.preventDefault();
        if (pending) return;
        button.disabled = pending = true;
        progress.hidden = false;
        spinner.hidden = false;
        outcome.textContent = '';
        message.textContent = 'Checking PCS race…';
        link.hidden = true;
        try {
            const data = await read(await fetch(form.action, {method: 'POST', body: new FormData(form), headers: {Accept: 'application/json'}}));
            if (data.existing) window.location.assign(data.race_url);
            else {
                message.textContent = `Fetching PCS race · Taskgroup #${data.group_id}…`;
                await poll(data.status_url);
            }
        } catch (error) { finish({error: error.message}); }
    });
})();
