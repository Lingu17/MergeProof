def checkout(order):
    rates = {"EUR": 1.08, "USD": 1.0}
    return round(order["subtotal"] * rates[order["currency"]], 2)
