using System.Collections.Generic;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Mvc.RazorPages;

namespace Shop.Pages
{
    public class OrdersModel : PageModel
    {
        public string Title { get; set; } = "Orders";
        public bool IsAdmin { get; set; }
        public List<Order> Orders { get; set; } = new List<Order>();

        [BindProperty]
        public string Email { get; set; }

        public void OnGet()
        {
        }

        public IActionResult OnPostSave()
        {
            return RedirectToPage("/Orders");
        }
    }

    public class Order
    {
        public int Id { get; set; }
    }
}
