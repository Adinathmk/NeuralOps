def extract_error_message(error):

    if hasattr(error, "detail"):

        # DRF list errors
        if isinstance(error.detail, list):
            return str(error.detail[0])

        # DRF dict errors
        elif isinstance(error.detail, dict):

            first_key = next(iter(error.detail))
            first_value = error.detail[first_key]

            if isinstance(first_value, list):
                return str(first_value[0])

            return str(first_value)

        # plain detail
        return str(error.detail)

    return str(error)
