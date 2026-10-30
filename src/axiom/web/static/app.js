// Progressive enhancement for /app. Without JavaScript every form posts and
// redirects as usual; with it, forms post in place and the page the redirect
// lands on is swapped in, so the list keeps its scroll position.
//
// Anything slower than a blink shows a progress line under the header;
// leaving the page also swaps the content for a skeleton, since an installed
// app has no browser loading bar. The undo bar fades after a few seconds and
// its query parameter goes with it, so a reload doesn't bring it back.

const NOTICE_MS = 6000;
const BUSY_DELAY_MS = 120;

let busyTimer;
let noticeTimer;

function fadeNotice() {
  clearTimeout(noticeTimer);
  const notice = document.querySelector('.notice');
  if (!notice) return;
  noticeTimer = setTimeout(() => {
    notice.classList.add('gone');
    setTimeout(() => notice.remove(), 300);
    const url = new URL(location.href);
    url.searchParams.delete('undo');
    url.searchParams.delete('forgot');
    history.replaceState(null, '', url.pathname + url.search);
  }, NOTICE_MS);
}

function busy(on, leaving) {
  clearTimeout(busyTimer);
  const classes = document.documentElement.classList;
  if (!on) {
    classes.remove('busy', 'nav');
    return;
  }
  busyTimer = setTimeout(() => {
    classes.add('busy');
    if (leaving) classes.add('nav');
  }, BUSY_DELAY_MS);
}

function isPlainClick(e, link) {
  return !(
    e.defaultPrevented || e.button || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey ||
    link.target || link.hasAttribute('download')
  );
}

document.addEventListener('click', (e) => {
  const link = e.target.closest('a[href]');
  if (!link || !isPlainClick(e, link)) return;
  const url = new URL(link.href, location.href);
  if (url.origin !== location.origin || !url.pathname.startsWith('/app')) return;
  if (link.closest('nav')) {
    document.querySelectorAll('nav a').forEach((a) => a.classList.toggle('on', a === link));
  }
  busy(true, true);
});

document.addEventListener('submit', async (e) => {
  const form = e.target;
  if (e.defaultPrevented || !form.closest('main')) return;
  if (form.method !== 'post') {
    busy(true, true);
    return;
  }
  e.preventDefault();
  const body = new FormData(form);
  const row = form.closest('.task');
  if (row && form.action.endsWith('/done')) {
    row.classList.add('leaving');
  } else if (e.submitter) {
    e.submitter.classList.add('pending');
    e.submitter.disabled = true;
  }
  const isQuickAdd = form.classList.contains('add');
  busy(true);
  try {
    const response = await fetch(form.action, { method: 'POST', body });
    const url = new URL(response.url);
    if (!response.ok || url.pathname !== location.pathname) {
      location.href = response.url;
      return;
    }
    const next = new DOMParser().parseFromString(await response.text(), 'text/html');
    const y = scrollY;
    document.querySelector('main').replaceWith(next.querySelector('main'));
    history.replaceState(null, '', url.pathname + url.search);
    scrollTo(0, y);
    fadeNotice();
    if (isQuickAdd) document.querySelector('main form.add input[name=title]')?.focus();
    busy(false);
  } catch (_) {
    form.submit();
  }
});

addEventListener('pageshow', () => busy(false));
fadeNotice();
