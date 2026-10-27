// Classic wrapper: the `$` parameter is supplied by jQuery itself.
(function ($) {
    $.post("/api/iife", { a: 1 });
})(jQuery);
