package shop;

import com.opensymphony.xwork2.ActionSupport;

public class OrderAction extends ActionSupport {
    private String email;

    public String save() {
        return SUCCESS;
    }

    public String getEmail() { return email; }
    public void setEmail(String email) { this.email = email; }
}
