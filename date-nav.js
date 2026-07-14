(function () {
    'use strict';

    function formatDate(date) {
        if (!/^\d{8}$/.test(date || '')) return date || '-';
        return `${date.slice(0, 4)}-${date.slice(4, 6)}-${date.slice(6, 8)}`;
    }

    function monthLabel(month) {
        return `${month.slice(0, 4)}年${Number(month.slice(4, 6))}月`;
    }

    class DateNavigator {
        constructor(container) {
            this.container = container;
            this.select = container.querySelector('select');
            this.dates = [];
            this.months = [];
            this.visibleMonth = '';
            this.isOpen = false;
            if (!this.select) return;

            this.build();
            this.observe();
            this.sync();
        }

        build() {
            this.container.classList.add('is-enhanced');
            this.controls = document.createElement('div');
            this.controls.className = 'date-nav-controls';
            this.controls.innerHTML = `
                <button type="button" class="date-nav-icon" data-action="older" title="前一交易日" aria-label="前一交易日">‹</button>
                <button type="button" class="date-nav-current" data-action="toggle" aria-expanded="false">
                    <span data-role="current-date">-</span>
                </button>
                <button type="button" class="date-nav-icon" data-action="newer" title="后一交易日" aria-label="后一交易日">›</button>
                <button type="button" class="date-nav-latest" data-action="latest">最新</button>
                <div class="date-nav-popover" data-role="popover" hidden>
                    <div class="date-nav-monthbar">
                        <button type="button" class="date-nav-icon" data-action="older-month" title="上一个有数据的月份" aria-label="上一个有数据的月份">‹</button>
                        <strong data-role="month-label">-</strong>
                        <button type="button" class="date-nav-icon" data-action="newer-month" title="下一个有数据的月份" aria-label="下一个有数据的月份">›</button>
                    </div>
                    <div class="date-nav-weekdays" aria-hidden="true">
                        <span>日</span><span>一</span><span>二</span><span>三</span><span>四</span><span>五</span><span>六</span>
                    </div>
                    <div class="date-nav-calendar" data-role="calendar"></div>
                </div>
            `;
            this.select.insertAdjacentElement('afterend', this.controls);

            this.controls.addEventListener('click', (event) => {
                const button = event.target.closest('button');
                if (!button || button.disabled) return;
                this.handleAction(button.dataset.action, button.dataset.date);
            });
            this.select.addEventListener('change', () => this.sync());
            document.addEventListener('click', (event) => {
                if (this.isOpen && !this.container.contains(event.target)) this.close();
            });
            document.addEventListener('keydown', (event) => {
                if (event.key === 'Escape' && this.isOpen) this.close();
            });
        }

        observe() {
            this.observer = new MutationObserver(() => this.sync());
            this.observer.observe(this.select, {
                childList: true,
                attributes: true,
                attributeFilter: ['disabled']
            });
        }

        getSelectedDate() {
            return this.dates.includes(this.select.value) ? this.select.value : (this.dates[0] || '');
        }

        sync() {
            this.dates = Array.from(this.select.options)
                .map((option) => option.value)
                .filter((value) => /^\d{8}$/.test(value));
            this.dates = [...new Set(this.dates)].sort((a, b) => b.localeCompare(a));
            this.months = [...new Set(this.dates.map((date) => date.slice(0, 6)))];

            const selected = this.getSelectedDate();
            const index = this.dates.indexOf(selected);
            this.controls.querySelector('[data-role="current-date"]').textContent = formatDate(selected);
            this.controls.querySelector('[data-action="older"]').disabled = index < 0 || index >= this.dates.length - 1;
            this.controls.querySelector('[data-action="newer"]').disabled = index <= 0;
            this.controls.querySelector('[data-action="latest"]').disabled = index <= 0;
            this.controls.querySelector('[data-action="toggle"]').disabled = !this.dates.length || this.select.disabled;

            if (!this.visibleMonth || !this.months.includes(this.visibleMonth)) {
                this.visibleMonth = selected.slice(0, 6) || this.months[0] || '';
            }
            if (this.isOpen) this.renderCalendar();
        }

        handleAction(action, date) {
            const selected = this.getSelectedDate();
            const index = this.dates.indexOf(selected);
            if (action === 'older') this.choose(this.dates[index + 1]);
            if (action === 'newer') this.choose(this.dates[index - 1]);
            if (action === 'latest') this.choose(this.dates[0]);
            if (action === 'choose') this.choose(date);
            if (action === 'toggle') this.isOpen ? this.close() : this.open();
            if (action === 'older-month') this.moveMonth(1);
            if (action === 'newer-month') this.moveMonth(-1);
        }

        choose(date) {
            if (!date || !this.dates.includes(date) || date === this.select.value) {
                this.close();
                return;
            }
            this.select.value = date;
            this.select.dispatchEvent(new Event('change', { bubbles: true }));
            this.close();
            this.sync();
        }

        open() {
            this.isOpen = true;
            this.visibleMonth = this.getSelectedDate().slice(0, 6) || this.months[0] || '';
            this.controls.querySelector('[data-role="popover"]').hidden = false;
            this.controls.querySelector('[data-action="toggle"]').setAttribute('aria-expanded', 'true');
            this.renderCalendar();
        }

        close() {
            this.isOpen = false;
            this.controls.querySelector('[data-role="popover"]').hidden = true;
            this.controls.querySelector('[data-action="toggle"]').setAttribute('aria-expanded', 'false');
        }

        moveMonth(offset) {
            const index = this.months.indexOf(this.visibleMonth);
            const target = this.months[index + offset];
            if (!target) return;
            this.visibleMonth = target;
            this.renderCalendar();
        }

        renderCalendar() {
            const monthIndex = this.months.indexOf(this.visibleMonth);
            const monthDates = new Set(this.dates.filter((date) => date.startsWith(this.visibleMonth)));
            const selected = this.getSelectedDate();
            const year = Number(this.visibleMonth.slice(0, 4));
            const month = Number(this.visibleMonth.slice(4, 6));
            const firstWeekday = new Date(year, month - 1, 1).getDay();
            const dayCount = new Date(year, month, 0).getDate();
            const cells = [];

            for (let index = 0; index < firstWeekday; index += 1) {
                cells.push('<span class="date-nav-empty"></span>');
            }
            for (let day = 1; day <= dayCount; day += 1) {
                const date = `${this.visibleMonth}${String(day).padStart(2, '0')}`;
                if (!monthDates.has(date)) {
                    cells.push(`<span class="date-nav-day is-disabled">${day}</span>`);
                    continue;
                }
                const selectedClass = date === selected ? ' is-selected' : '';
                cells.push(`<button type="button" class="date-nav-day${selectedClass}" data-action="choose" data-date="${date}">${day}</button>`);
            }

            this.controls.querySelector('[data-role="month-label"]').textContent = monthLabel(this.visibleMonth);
            this.controls.querySelector('[data-role="calendar"]').innerHTML = cells.join('');
            this.controls.querySelector('[data-action="older-month"]').disabled = monthIndex < 0 || monthIndex >= this.months.length - 1;
            this.controls.querySelector('[data-action="newer-month"]').disabled = monthIndex <= 0;
        }
    }

    document.addEventListener('DOMContentLoaded', () => {
        document.querySelectorAll('.date-selector').forEach((container) => new DateNavigator(container));
    });
}());
