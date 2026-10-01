function handler(event) {
    var request = event.request;
    var prefix = '{{ prefix }}';
    if (request.uri.indexOf(prefix) === 0) {
        var rest = request.uri.substring(prefix.length);
        var slash = rest.indexOf('/');
        if (slash > 0) {
            var segment = rest.substring(0, slash);
            if ({{ segment_condition }}) {
                request.uri = prefix + rest.substring(slash + 1);
            }
        }
    }
    return request;
}
