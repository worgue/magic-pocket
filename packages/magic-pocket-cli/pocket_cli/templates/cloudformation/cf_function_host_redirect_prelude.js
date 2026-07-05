    var __pocketHost = request.headers.host && request.headers.host.value;
    if (__pocketHost && __pocketHost !== '{{ canonical_domain }}') {
        var __pocketQs = '';
        for (var __pocketKey in request.querystring) {
            var __pocketVal = request.querystring[__pocketKey].value;
            __pocketQs += (__pocketQs ? '&' : '?') + __pocketKey + (__pocketVal !== '' ? '=' + __pocketVal : '');
        }
        return {
            statusCode: 301,
            statusDescription: 'Moved Permanently',
            headers: { location: { value: 'https://{{ canonical_domain }}' + request.uri + __pocketQs } }
        };
    }
