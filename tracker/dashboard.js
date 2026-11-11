const trendTabs = document.querySelectorAll('.trend-tabs a[data-trend-group]');

for (const tab of trendTabs) {
  tab.addEventListener('click', (event) => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    const group = tab.dataset.trendGroup;
    if (tab.hasAttribute('aria-current')) return;

    for (const view of document.querySelectorAll('[data-trend-view]')) {
      view.hidden = view.dataset.trendView !== group;
    }
    for (const choice of trendTabs) {
      if (choice === tab) choice.setAttribute('aria-current', 'page');
      else choice.removeAttribute('aria-current');
    }
    for (const link of document.querySelectorAll('a[href^="/"]:not([data-trend-group])')) {
      const url = new URL(link.getAttribute('href'), location.href);
      if (group === 'month') url.searchParams.set('group', 'month');
      else url.searchParams.delete('group');
      link.setAttribute('href', url.pathname + url.search + url.hash);
    }
    for (const form of document.querySelectorAll('.period-filter form, .order-search')) {
      form.querySelector('input[name="group"]')?.remove();
      if (group === 'month') {
        const input = document.createElement('input');
        input.type = 'hidden';
        input.name = 'group';
        input.value = 'month';
        form.append(input);
      }
    }
    history.replaceState(null, '', tab.href);
  });
}

document.querySelector('[data-add-item]')?.addEventListener('click', () => {
  const basket = document.querySelector('[data-basket-items]');
  const template = basket?.querySelector('[data-basket-item]:last-child');
  if (!template) return;
  const position = basket.querySelectorAll('[data-basket-item]').length;
  const row = template.cloneNode(true);
  row.querySelector('strong').textContent = `Item ${position + 1}`;
  for (const field of row.querySelectorAll('input, select')) {
    field.name = field.name.replace(/_\d+$/, `_${position}`);
    if (field.tagName === 'INPUT') field.value = field.name.startsWith('item_quantity_') ? '1' : '';
    else field.value = 'Other';
  }
  basket.append(row);
  row.querySelector('input[name^="item_name_"]')?.focus();
});

for (const input of document.querySelectorAll('[data-refund-order]')) {
  const container = input.closest('form')?.querySelector('[data-refund-items]');
  if (!container) continue;
  const existing = JSON.parse(container.dataset.existing || '{}');
  const initialOrder = input.value;
  const loadItems = async () => {
    container.replaceChildren();
    if (!input.value.trim()) return;
    try {
      const response = await fetch(`/order-items?order=${encodeURIComponent(input.value.trim())}`);
      if (!response.ok) throw new Error('Could not load order items');
      const items = await response.json();
      if (!items.length) {
        container.textContent = 'No items found for this order; refund stays unallocated.';
        return;
      }
      const heading = document.createElement('strong');
      heading.textContent = 'Returned item amounts (optional)';
      container.append(heading);
      for (const item of items) {
        const label = document.createElement('label');
        label.textContent = `${item.name} · ${item.category}`;
        const amount = document.createElement('input');
        amount.name = `refund_item_${item.position}`;
        amount.type = 'number';
        amount.min = '0';
        amount.step = '0.01';
        amount.placeholder = '₱ amount returned';
        amount.value = input.value === initialOrder ? (existing[item.position] || '') : '';
        label.append(amount);
        container.append(label);
      }
      const note = document.createElement('small');
      note.textContent = 'Enter item amounts totaling the refund to assign categories. Leave blank to keep it unallocated.';
      container.append(note);
    } catch {
      container.textContent = 'Could not load order items. You can still save the refund unallocated.';
    }
  };
  let timer;
  input.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(loadItems, 200);
  });
  input.addEventListener('change', loadItems);
  if (input.value) loadItems();
}
