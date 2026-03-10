import cf from 'cloudfront';
var crypto = require('crypto');
const kvsHandle = cf.kvs('${TokenKvs}');
async function handler(event) {
    var request = event.request;
    var originalUri = request.uri;
    var lastItem = request.uri.split('/').pop();
    if (!lastItem.includes('.')) { request.uri = '{{ fallback_uri }}'; }
    var cookie = request.cookies['pocket-spa-token'];
    if (!cookie) { return _redirect(originalUri); }
    var parts = cookie.value.split(':');
    if (parts.length !== 3) { return _redirect(originalUri); }
    var expiry = parseInt(parts[1], 10);
    if (Math.floor(Date.now() / 1000) > expiry) { return _redirect(originalUri); }
    var secret;
    try { secret = await kvsHandle.get('token_secret'); }
    catch (e) { return _redirect(originalUri); }
    var msg = parts[0] + ':' + parts[1];
    var hmac = crypto.createHmac('sha256', Buffer.from(secret, 'hex'));
    var sig = hmac.update(msg).digest('hex');
    if (sig !== parts[2]) { return _redirect(originalUri); }
    return request;
}
function _redirect(uri) {
    var next = encodeURIComponent(uri);
    return { statusCode: 302, statusDescription: 'Found',
        headers: { location: { value: '{{ login_path }}?next=' + next } } };
}
