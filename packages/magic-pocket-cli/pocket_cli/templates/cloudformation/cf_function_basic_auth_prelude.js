    var __pocketBaAuth = request.headers.authorization;
    var __pocketBaExpected;
    try { __pocketBaExpected = await kvsHandle.get('basic_auth'); }
    catch (e) { __pocketBaExpected = null; }
    if (!__pocketBaExpected || !__pocketBaAuth || __pocketBaAuth.value !== __pocketBaExpected) {
        return {
            statusCode: 401,
            statusDescription: 'Unauthorized',
            headers: { 'www-authenticate': { value: 'Basic realm="Restricted"' } }
        };
    }
