from enum import IntEnum


# LISTING SOURCE
class ListingSource(IntEnum):
    """
    Enumeration of listing sources with their corresponding database IDs.
    """

    AUTOTRADER = 1
    EBAY = 2


# LISTING TABLE
class ListingTable(IntEnum):
    """
    Enumeration of listing tables with their corresponding database IDs.
    """

    PROSPECT = 1
    ADVERTISED = 2
