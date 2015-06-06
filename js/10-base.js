Liberapay = {};

Liberapay.getCookie = function(key) {
    var o = new RegExp("(?:^|; ?)" + escape(key) + "=([^;]+)").exec(document.cookie);
    return o && unescape(o[1]);
}

Liberapay.init = function() {
    // https://docs.djangoproject.com/en/dev/ref/contrib/csrf/#ajax
    jQuery.ajaxSetup({
        beforeSend: function(xhr, settings) {
            var safeMethod = (/^(GET|HEAD|OPTIONS|TRACE)$/.test(settings.type));
            if (!safeMethod && !settings.crossDomain) {
                // We have to avoid httponly on the csrf_token cookie because of this.
                xhr.setRequestHeader("X-CSRF-TOKEN", Liberapay.getCookie('csrf_token'));
            }
        }
    });

    $('#jump').submit(function (e) {
        e.preventDefault();
        var platform = $('#jump select').val().trim(),
            user_name = $('#jump input').val().trim();
        if (user_name) window.location = '/on/' + platform + '/' + user_name + '/';
    });

    Liberapay.forms.jsSubmit();
};

Liberapay.error = function(jqXHR, textStatus, errorThrown) {
    var msg = null;
    try {
        msg = JSON.parse(jqXHR.responseText).error_message_long;
    } catch(exc) {}
    if(!msg) {
        msg = "An error occurred (" + (errorThrown || textStatus) + ").\n" +
              "Please contact support@liberapay.com if the problem persists.";
    }
    Liberapay.notification(msg, 'error', -1);
}

Liberapay.jsonml = function(jsonml) {
    var node  = document.createElement(jsonml[0]);

    jQuery.each(jsonml, function(j, v) {
        if (j === 0 || typeof v === 'undefined') return;

        switch (v.constructor) {
            case Object:
                for (var p in v)
                    node.setAttribute(p, v[p]);
                break;

            case Array: node.appendChild(Liberapay.jsonml(v)); break;

            case String: case Number:
                node.appendChild(document.createTextNode(v));
                break;

            default: node.appendChild(v); break;
        }
    });

    return node;
};
