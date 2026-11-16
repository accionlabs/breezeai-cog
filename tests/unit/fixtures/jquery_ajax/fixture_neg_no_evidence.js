// `$` with no jQuery import / script reference: only the options-object exception may fire.
$.get("/api/items");
$.post("/api/save", data);
$("#box").load("/partial");
$.ajax(buildOptions());
