# Amounts are expressed in the selected order's currency, not minor units.
# Bind this policy to issue_refund with amount and currency selected.
package openeight.policy

default decision := "deny"

refund_limits := {
    "USD": {"allow": 50, "approval": 200},
    "EUR": {"allow": 40, "approval": 200},
    "JPY": {"allow": 5000, "approval": 20000},
}

decision := "allow" if {
    amount := input.structured_fields.amount
    currency := input.structured_fields.currency
    is_number(amount)
    is_string(currency)
    amount > 0
    amount <= refund_limits[currency].allow
}

decision := "approval_required" if {
    amount := input.structured_fields.amount
    currency := input.structured_fields.currency
    is_number(amount)
    is_string(currency)
    amount > refund_limits[currency].allow
    amount <= refund_limits[currency].approval
}

deny_reason := "Refund amount or currency is invalid or exceeds the permitted maximum." if {
    decision == "deny"
}
