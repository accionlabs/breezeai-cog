const $ = customLibrary;

$.get("/api/items");
$.ajax({ url: "/api/custom", type: "POST" });
$("#box").load("/partial");
