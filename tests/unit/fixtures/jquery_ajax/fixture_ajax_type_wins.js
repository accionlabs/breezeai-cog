// When both are present, `type` wins over `method`.
$.ajax({ method: "PUT", url: "/api/both", type: "DELETE" });
