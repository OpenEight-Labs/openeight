# Bind issue_refund with order_id, amount and currency selected.
package openeight.policy

default decision := "deny"

valid_refund if {
    is_number(input.structured_fields.amount)
    input.structured_fields.amount > 0
    regex.match("^[A-Z]{3}$", input.structured_fields.currency)
}

decision := "allow" if {
    valid_refund
    input.structured_fields.currency == "USD"
    input.structured_fields.amount <= 100
}

decision := "approval_required" if {
    valid_refund
    input.structured_fields.currency == "USD"
    input.structured_fields.amount > 100
}

decision := "approval_required" if {
    valid_refund
    input.structured_fields.currency != "USD"
}
