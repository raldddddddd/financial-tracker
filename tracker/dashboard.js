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

const orders = document.querySelector('#orders');
const searchForm = orders?.querySelector('.order-search');
let orderRequest;

async function showOrders(url, addHistory = true) {
  orderRequest?.abort();
  const request = new AbortController();
  orderRequest = request;
  orders.setAttribute('aria-busy', 'true');
  try {
    // shortcut: fetches the full dashboard; add an orders-only response if searches become slow.
    const response = await fetch(url, { signal: request.signal });
    if (!response.ok) throw new Error('Could not load orders');
    const nextPage = new DOMParser().parseFromString(await response.text(), 'text/html');
    if (request !== orderRequest) return;
    const next = nextPage.querySelector('#orders');
    if (!next) throw new Error('Orders missing from response');

    const heading = orders.querySelector('.section-heading');
    heading.querySelector('.section-meta').textContent = next.querySelector('.section-meta').textContent;
    if (heading.querySelector('.insight-note')) {
      heading.querySelector('.insight-note').textContent = next.querySelector('.insight-note').textContent;
    }
    orders.querySelector('tbody').replaceWith(next.querySelector('tbody'));
    orders.querySelector('.pagination')?.remove();
    if (next.querySelector('.pagination')) orders.append(next.querySelector('.pagination'));
    searchForm.querySelector('a')?.remove();
    if (next.querySelector('.order-search a')) searchForm.append(next.querySelector('.order-search a'));
    searchForm.querySelector('input[name="q"]').value = next.querySelector('input[name="q"]').value;

    for (const selector of ['.shop-filter a', '.period-filter a', '.category-card a', '.trend-tabs a', '.orders-card > .category-reset']) {
      const links = document.querySelectorAll(selector);
      const newLinks = nextPage.querySelectorAll(selector);
      links.forEach((link, index) => link.setAttribute('href', newLinks[index].getAttribute('href')));
    }
    for (const form of document.querySelectorAll('.period-filter form')) {
      form.querySelector('input[name="q"]')?.remove();
      const value = nextPage.querySelector(`.period-filter .${form.className} input[name="q"]`);
      if (value) form.append(value);
    }
    if (addHistory) history.pushState(null, '', url);
    if (addHistory) searchForm.querySelector('input[name="q"]').focus({ preventScroll: true });
  } catch (error) {
    if (error.name !== 'AbortError') location.assign(url);
  } finally {
    if (request === orderRequest) orders.removeAttribute('aria-busy');
  }
}

searchForm?.addEventListener('submit', (event) => {
  event.preventDefault();
  const url = new URL(searchForm.action);
  url.search = new URLSearchParams(new FormData(searchForm));
  if (!url.searchParams.get('q')) url.searchParams.delete('q');
  showOrders(url);
});

orders?.addEventListener('click', (event) => {
  const link = event.target.closest('.order-search a');
  if (!link || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  showOrders(link.href);
});

window.addEventListener('popstate', () => {
  if (orders) showOrders(location.href, false);
});

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
  let requestId = 0;
  const loadItems = async () => {
    const currentRequest = ++requestId;
    const order = input.value.trim();
    container.replaceChildren();
    if (!order) return;
    container.textContent = 'Loading order items…';
    try {
      const response = await fetch(`/order-items?order=${encodeURIComponent(order)}`);
      if (!response.ok) throw new Error('Could not load order items');
      const items = await response.json();
      if (currentRequest !== requestId) return;
      container.replaceChildren();
      if (!items.length) {
        container.textContent = 'No items found for this order; its category is unallocated.';
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
        amount.value = order === initialOrder ? (existing[item.position] || '') : '';
        label.append(amount);
        container.append(label);
      }
      const note = document.createElement('small');
      note.textContent = 'Enter item amounts totaling the refund for mixed-category orders. Single-category refunds follow the purchase category.';
      container.append(note);
    } catch {
      if (currentRequest === requestId) container.textContent = 'Could not load order items. You can still save the refund.';
    }
  };
  let timer;
  input.addEventListener('input', () => {
    ++requestId;
    container.replaceChildren();
    clearTimeout(timer);
    timer = setTimeout(loadItems, 200);
  });
  if (input.value) loadItems();
}
