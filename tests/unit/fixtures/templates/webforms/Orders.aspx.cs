using System;
using System.Web.UI;

namespace Shop
{
    public partial class Orders : Page
    {
        protected void Save_Click(object sender, EventArgs e)
        {
            Response.Redirect("~/Done.aspx");
        }
    }
}
