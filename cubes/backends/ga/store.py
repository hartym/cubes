# -*-coding=utf -*-

"""Google Analytics backend for Cubes

Required packages:

    pyopenssl
    google-api-python-client

"""

from ...errors import *
from ...logging import get_logger
from ...stores import Store
from ...providers import ModelProvider
from ...model import Cube, create_dimension, aggregate_list
from .mapper import ga_id_to_identifier
import pkgutil

from apiclient.errors import HttpError
from apiclient.discovery import build
from oauth2client.client import AccessTokenRefreshError

from collections import OrderedDict

import re

try:
    from oauth2client.client import SignedJwtAssertionCredentials
except ImportError:
    from ...common import MissingPackage
    SignedJwtAssertionCredentials = MissingPackage("oauth2client",
                                                    "Google Analytics Backend")

try:
    import httplib2
except ImportError:
    from ...common import MissingPackage
    httplib2 = MissingPackage("httplib2", "Google Analytics Backend")


GA_TIME_DIM_METADATA = {
    "name": "time",
    "role": "time",
    "levels": [
        { "name": "year", "label": "Year" },
        { "name": "month", "label": "Month", "info": { "aggregation_units": 3 }},
        { "name": "day", "label": "Day", "info": { "aggregation_units": 7 } },
    ],
    "hierarchies": [
        {"name": "ymd", "levels": ["year", "month", "day"]},
    ],
    "default_hierarchy_name": "ymd"
}


class GoogleAnalyticsModelProvider(ModelProvider):
    __identifier__ = "ga"
    def __init__(self, *args, **kwargs):
        super(GoogleAnalyticsModelProvider, self).__init__(*args, **kwargs)

        self.name_to_group = {}
        self.group_to_name = {}

    def requires_store(self):
        return True

    def public_dimensions(self):
        return []

    def initialize_from_store(self):
        self.name_to_group = {}
        self.group_to_name = {}

        for group in self.store.groups:
            name = re.sub("[^\w0-9_]", "_", group.lower())
            self.name_to_group[name] = group
            self.group_to_name[group] = name

    def cube(self, name, locale=None):
        """Create a GA cube:

        * cube is a GA group
        * GA metric is cube aggregate
        * GA dimension is cube dimension
        """

        # TODO: preliminary implementation

        try:
            metadata = self.cube_metadata(name)
        except NoSuchCubeError:
            metadata = {}

        group = self.name_to_group[name]

        cube = {
            "name": name,
            "label": metadata.get("label", group),
            "category": metadata.get("category")
        }

        # Gather aggregates

        metrics = [m for m in self.store.metrics.values() if m["group"] == group]

        aggregates = []
        for metric in metrics:
            aggregate = {
                "name": ga_id_to_identifier(metric["id"]),
                "label": metric["uiName"],
                "description": metric.get("description")
            }
            aggregates.append(aggregate)

        aggregates = aggregate_list(aggregates)

        dims = [d for d in self.store.dimensions.values() if d["group"] == group]
        dims = [ga_id_to_identifier(d["id"]) for d in dims]
        dims = ["time"] + dims

        cube = Cube(name=name,
                    label=metadata.get("label", group),
                    aggregates=aggregates,
                    category=metadata.get("category"),
                    info=metadata.get("info"),
                    linked_dimensions=dims,
                    datastore=self.store_name)

        return cube

    def dimension(self, name, dimensions=[], locale=None):
        try:
            metadata = self.dimension_metadata(name)
        except KeyError:
            metadata = {}

        if name == "time":
            return create_dimension(GA_TIME_DIM_METADATA)

        # TODO: this should be in the mapper
        ga_id = "ga:" + name

        try:
            ga_dim = self.store.dimensions[ga_id]
        except KeyError:
            raise NoSuchDimensionError("No GA dimension %s" % name,
                                       name=name)

        dim = {
            "name": name,
            "label": metadata.get("label", ga_dim["uiName"]),
            "description": metadata.get("description", ga_dim["description"])
        }

        return create_dimension(dim)

    def list_cubes(self):
        """List GA cubes – groups of metrics and dimensions."""
        # TODO: use an option how to look at GA – what are cubes?

        cubes = []
        for group in self.store.groups:
            name = self.group_to_name[group]

            try:
                metadata = self.cube_metadata(name)
            except NoSuchCubeError:
                metadata = {}

            cube = {
                "name": name,
                "label": metadata.get("label", group),
                "category": metadata.get("category")
            }
            cubes.append(cube)

        return cubes


class GoogleAnalyticsStore(Store):
    __identifier__ = "ga"

    def __init__(self, email=None, key_file=None, account_id=None,
                 account_name=None, web_property=None, **options):

        self.logger = get_logger()

        self.service = None
        self.credentials = None

        if not email:
            raise ConfigurationError("Google Analytics: email is required")
        if not key_file:
            raise ConfigurationError("Google Analytics: key_file is required")

        if account_name and account_id:
            raise ConfigurationError("Both account_name and account_id "
                                     "provided. Use only one or none.")

        with open(key_file) as f:
            self.key = f.read()

        self.email = email
        self.web_property = web_property

        self.account_id = None
        self.credentials = SignedJwtAssertionCredentials(self.email,
                              self.key,
                              scope="https://www.googleapis.com/auth/analytics.readonly")

        # TODO: make this lazy

        self._authorize()
        self._initialize_account(account_name, account_id)


        # Note: The dimensions here are GA dimensions not Cubes dimensions
        self.metrics = []
        self.dimensions = []
        self.refresh_metadata()

    def _authorize(self):
        self.logger.debug("Authorizing GA")
        http = httplib2.Http()
        http = self.credentials.authorize(http)
        self.service = build('analytics', 'v3', http=http)

    def _initialize_account(self, account_name, account_id):

        accounts = self.service.management().accounts().list().execute()

        self.account = None
        if account_id:
            key = "id"
            value = account_id
        elif account_name:
            key = "name"
            value = account_name
        else:
            # If no ID or account name are provided, use the first account
            self.account = accounts["items"][0]

        if not self.account:
            for account in accounts['items']:
                if account[key] == value:
                    self.account = account
                    break

        if not self.account:
            raise ConfigurationError("Unknown GA account with %s='%s'" %
                                     (key, value))

        self.account_id = self.account["id"]
        self.logger.debug("Using GA account id %s" % self.account_id)

        if not self.web_property:
            base = self.service.management().webproperties()
            props = base.list(accountId=self.account_id).execute()
            self.web_property = props["items"][0]["id"]

        base = self.service.management().profiles()
        profiles = base.list(accountId=self.account_id,
                           webPropertyId=self.web_property).execute()
        self.profile_id = profiles["items"][0]["id"]

    def refresh_metadata(self):
        """Load GA metadata. Group metrics and dimensions by `group`"""
        md = self.service.metadata().columns().list(reportType='ga').execute()

        # Note: no Cubes model related logic should be here (such as name
        # mangling)

        self.metrics = OrderedDict()
        self.dimensions = OrderedDict()
        self.groups = []

        for item in md["items"]:
            # Get the id from the "outer" dictionary and keep juts the "inner"
            # dictionary
            item_id = item["id"]
            item = item["attributes"]
            item["id"] = item_id

            if item["type"] == "METRIC":
                self.metrics[item_id] = item
            elif item["type"] == "DIMENSION":
                self.dimensions[item_id] = item
            else:
                self.logger.debug("Unknown metadata item type: %s (id: %s)"
                                  % (item["type"], item["id"]))

            group = item["group"]
            if group not in self.groups:
                self.groups.append(group)

    def get_data(self, **kwargs):
        ga = self.service.data().ga()

        try:
            response = ga.get(ids='ga:%s' % self.profile_id,
                              **kwargs).execute()
        except TypeError as e:
            raise ArgumentError("Google Analytics Error: %s"
                                % str(e))
        except HttpError as e:
            raise BrowserError("Google Analytics HTTP Error: %s"
                                % str(e))
        except AccessTokenRefreshError as e:
            raise NotImplementedError("Re-authorization not implemented yet")

        return response
