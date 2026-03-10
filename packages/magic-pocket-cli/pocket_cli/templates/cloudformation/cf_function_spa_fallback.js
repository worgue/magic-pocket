function handler(event) {
    var request = event.request;
    var lastItem = request.uri.split('/').pop();
    if (!lastItem.includes('.')) {
        request.uri = '{{ fallback_uri }}';
    }
    return request;
}
