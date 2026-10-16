def checkout(order):
    discount = order["subtotal"] * 0.10 if order.get("loyalty") else 0
    return round(order["subtotal"] - discount, 2)
