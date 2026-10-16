def checkout(order):
    rates = {"EUR": 1.08, "USD": 1.0}
    order["total_usd"] = round(order["subtotal"] * rates[order["currency"]], 2)
    discount = order["subtotal"] * 0.10 if order.get("loyalty") else 0
    return round(order["total_usd"] - discount, 2)
