import {readFileSync} from 'node:fs';

const collector = readFileSync(new URL('./taobao_capture.js', import.meta.url), 'utf8');
const productHosts = new Set(['item.taobao.com', 'detail.tmall.com']);

export async function readTaobaoProduct(page, sourceUrl, {timeout = 30000} = {}) {
  const requested = new URL(sourceUrl);
  if (requested.protocol !== 'https:' || !productHosts.has(requested.hostname) || requested.username || requested.password ||
      requested.searchParams.getAll('id').length !== 1 || !/^\d+$/.test(requested.searchParams.get('id') || '')) {
    throw new Error('INVALID_SOURCE_URL');
  }
  const failure = () => ({ok: false, reason: new URL(page.url()).hostname === 'login.taobao.com'
    ? 'LOGIN_REQUIRED' : 'SOURCE_NOT_READY'});
  try {
    await page.goto(requested.href, {waitUntil: 'domcontentloaded', timeout});
    if (!productHosts.has(new URL(page.url()).hostname)) return failure();
    await page.waitForFunction(`() => {
      if (!['item.taobao.com', 'detail.tmall.com'].includes(location.hostname)) return true;
      const result = (${collector})();
      return result.ok || ['DESCRIPTION_PAGE_REQUIRED', 'LOGIN_REQUIRED', 'EFFECTIVE_PRICE_UNVERIFIED'].includes(result.reason);
    }`, null, {timeout});
  } catch {
    return failure();
  }
  if (!productHosts.has(new URL(page.url()).hostname)) return failure();
  const initial = await page.evaluate(`(${collector})()`);
  if (initial.ok || initial.reason !== 'DESCRIPTION_PAGE_REQUIRED') return initial;
  const descriptionPage = await page.context().newPage();
  try {
    await descriptionPage.goto(initial.descriptionUrl, {waitUntil: 'domcontentloaded', timeout});
    if (new URL(descriptionPage.url()).hostname === 'login.taobao.com') return {ok: false, reason: 'LOGIN_REQUIRED'};
    await descriptionPage.waitForSelector('[data-name="singleImage"]', {state: 'attached', timeout});
    const description = await descriptionPage.evaluate(() => ({ok: true, pageUrl: location.href,
      images: [...document.querySelectorAll('[data-name="singleImage"]')].map(image => ({attrs: {
        'data-name': 'singleImage', 'data-src': image.getAttribute('data-src') || image.getAttribute('src') || ''
      }}))}));
    return await page.evaluate(`(${collector})(${JSON.stringify(description)})`);
  } catch {
    return {ok: false, reason: 'DESCRIPTION_NOT_READY'};
  } finally {
    await descriptionPage.close();
  }
}
