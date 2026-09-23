async page => {
  const requested = __MOVER_REQUEST__;
  const matches = response => {
    try {
      const address = /^https:\/\/api\.m\.jd\.com(?::443)?\/(?:ware\/)?image\/getPlayUrl\?([^#]*)/.exec(response.url());
      if (!address) return false;
      const parameters = Object.fromEntries(address[1].split('&').map(part => {
        const position = part.indexOf('=');
        return [decodeURIComponent(part.slice(0, position)), decodeURIComponent(part.slice(position + 1).replace(/\+/g, ' '))];
      }));
      const body = JSON.parse(parameters.body || '{}');
      return String(body.vid || parameters.vid || '') === requested.videoId;
    } catch {
      return false;
    }
  };
  const playback = page.waitForResponse(matches, {timeout: 25000}).then(async response => {
    if (!response.ok()) return {ok: false, reason: 'VIDEO_RESPONSE_DENIED'};
    return {ok: true, videoId: requested.videoId, text: await response.text()};
  }).catch(() => ({ok: false, reason: 'VIDEO_RESPONSE_UNAVAILABLE'}));
  try {
    const navigation = await page.goto(requested.url, {waitUntil: 'domcontentloaded', timeout: 25000});
    if (!navigation || !navigation.ok() || page.url().split(/[?#]/)[0] !== requested.url) {
      return {ok: false, reason: 'PAGE_IDENTITY_OR_ACCESS_CHANGED'};
    }
    return await playback;
  } catch {
    return {ok: false, reason: 'PAGE_UNAVAILABLE'};
  }
}
