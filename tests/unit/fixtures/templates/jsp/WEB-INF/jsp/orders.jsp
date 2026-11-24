<%@ page contentType="text/html;charset=UTF-8" import="java.util.List" %>
<%@ taglib prefix="s" uri="/struts-tags" %>
<%@ taglib prefix="c" uri="http://java.sun.com/jsp/jstl/core" %>
<%@ include file="/WEB-INF/jsp/header.jsp" %>
<%! private int counter = 0; %>
<html>
<body>
<h1>${title}</h1>
<s:form action="saveOrder" method="post">
    <s:textfield name="email" label="E-mail" />
    <s:submit value="Save" />
</s:form>
<form action="/shop/search" method="get">
    <label for="q">Search</label>
    <input id="q" name="q" type="text" />
</form>
<% if (request.isUserInRole("admin")) { %>
    <a href="admin.jsp">Admin</a>
<% } %>
<c:forEach var="order" items="${orders}">
    <p><c:out value="${order.id}" /></p>
</c:forEach>
<s:a action="listOrders">All orders</s:a>
<script>$.get('/shop/api/orders');</script>
<script>
    var API = '/shop/api/orders';
    fetch(API);
    fetch('${pageContext.request.contextPath}/shop/api/count');
</script>
</body>
</html>
