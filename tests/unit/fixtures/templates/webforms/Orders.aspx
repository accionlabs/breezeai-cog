<%@ Page Language="C#" MasterPageFile="~/Site.master" CodeBehind="Orders.aspx.cs" Inherits="Shop.Orders" %>
<%@ Register TagPrefix="uc" TagName="DateField" Src="~/Controls/DateField.ascx" %>
<asp:Content ID="Main" ContentPlaceHolderID="MainContent" runat="server">
    <uc:DateField ID="StartDate" runat="server" />
    <asp:TextBox ID="Email" runat="server" />
    <asp:Button ID="SaveButton" runat="server" Text="Save" OnClick="Save_Click" />
    <% if (User.IsInRole("Admin")) { %> <a href="/Admin.aspx">Admin</a> <% } %>
    <% if (Request.IsLocal) { %><div class="a"><% } else { %><div class="b"><% } %>
        <input type="text" name="note" />
    </div>
    <asp:GridView ID="OrdersGrid" runat="server" DataSourceID="OrdersDS">
        <Columns>
            <asp:TemplateField>
                <ItemTemplate><%# Eval("Id") %></ItemTemplate>
                <EditItemTemplate><asp:TextBox ID="Total" runat="server" Text='<%# Bind("Total") %>' /></EditItemTemplate>
            </asp:TemplateField>
        </Columns>
    </asp:GridView>
    <asp:SqlDataSource ID="OrdersDS" runat="server"
        SelectCommand="SELECT Id, Total FROM Orders"
        UpdateCommand="UPDATE Orders SET Total = @Total WHERE Id = @Id" />
    <asp:ObjectDataSource ID="RepoDS" runat="server" TypeName="Shop.OrderRepo" SelectMethod="GetAll" />
    <%-- an old comment --%>
    <script>$.post('/api/track', { page: 'orders' });</script>
</asp:Content>
<script runat="server">
    void Page_Load(object sender, EventArgs e)
    {
        Title = "Orders";
    }
</script>
