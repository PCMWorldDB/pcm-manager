document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('.stat-input-row .stat-input[type="number"]').forEach(function (input) {
        const controls = document.createElement('div');
        controls.className = 'stat-step-controls';
        [1, -1].forEach(function (step) {
            const button = document.createElement('button');
            button.type = 'button';
            button.textContent = step > 0 ? '+' : '−';
            button.setAttribute('aria-label', (step > 0 ? 'Increase ' : 'Decrease ') + input.dataset.statName);
            button.disabled = input.disabled;
            button.addEventListener('click', function () {
                if (input.disabled || input.readOnly) {
                    return;
                }
                if (step > 0) {
                    input.stepUp();
                } else {
                    input.stepDown();
                }
                input.dispatchEvent(new Event('input', { bubbles: true }));
                input.dispatchEvent(new Event('change', { bubbles: true }));
            });
            controls.appendChild(button);
        });
        input.parentElement.appendChild(controls);
    });
});
