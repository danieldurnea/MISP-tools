"""CrowdStrike Indicator MISP event import.

 _______                        __ _______ __        __ __
|   _   .----.-----.--.--.--.--|  |   _   |  |_.----|__|  |--.-----.
|.  1___|   _|  _  |  |  |  |  _  |   1___|   _|   _|  |    <|  -__|
|.  |___|__| |_____|________|_____|____   |____|__| |__|__|__|_____|
|:  1   |                         |:  1   |
|::.. . |                         |::.. . |
`-------'                         `-------'

@@@  @@@  @@@  @@@@@@@   @@@   @@@@@@@   @@@@@@   @@@@@@@   @@@@@@   @@@@@@@    @@@@@@
@@@  @@@@ @@@  @@@@@@@@  @@@  @@@@@@@@  @@@@@@@@  @@@@@@@  @@@@@@@@  @@@@@@@@  @@@@@@@
@@!  @@!@!@@@  @@!  @@@  @@!  !@@       @@!  @@@    @@!    @@!  @@@  @@!  @@@  !@@
!@!  !@!!@!@!  !@!  @!@  !@!  !@!       !@!  @!@    !@!    !@!  @!@  !@!  @!@  !@!
!!@  @!@ !!@!  @!@  !@!  !!@  !@!       @!@!@!@!    @!!    @!@  !@!  @!@!!@!   !!@@!!
!!!  !@!  !!!  !@!  !!!  !!!  !!!       !!!@!!!!    !!!    !@!  !!!  !!@!@!     !!@!!!
!!:  !!:  !!!  !!:  !!!  !!:  :!!       !!:  !!!    !!:    !!:  !!!  !!: :!!        !:!
:!:  :!:  !:!  :!:  !:!  :!:  :!:       :!:  !:!    :!:    :!:  !:!  :!:  !:!      !:!
 ::   ::   ::   :::: ::   ::   ::: :::  ::   :::     ::    ::::: ::  ::   :::  :::: ::
:    ::    :   :: :  :   :     :: :: :   :   : :     :      : :  :    :   : :  :: : :

"""
import datetime
import logging
import os

import concurrent.futures
from .confidence import MaliciousConfidence
from .helper import confirm_boolean_param, gen_indicator, INDICATORS_BANNER, display_banner
from .adversary import Adversary
from .kill_chain import KillChain
try:
    from pymisp import MISPObject, MISPEvent, MISPAttribute, ExpandedPyMISP, MISPTag
except ImportError as no_pymisp:
    raise SystemExit(
        "The PyMISP package must be installed to use this program."
        ) from no_pymisp

class IndicatorsImporter:
    """Tool used to import indicators from the Crowdstrike Intel API.

    Adds them as objects attached to the events in MISP coresponding to the Crowdstrike Intel Reports they are related to.

    :param misp_client: client for a MISP instance
    :param intel_api_client: client for the Crowdstrike Intel API
    """
    MISSING_GALAXIES = None
    def __init__(self,
                 misp_client,
                 intel_api_client,
                 crowdstrike_org_uuid,
                 indicators_timestamp_filename,
                 import_all_indicators,
                 delete_outdated,
                 settings,
                 import_settings,
                 logger
                 ):
        """Construct an instance of the IndicatorsImporter class."""
        self.misp: ExpandedPyMISP = misp_client
        self.intel_api_client = intel_api_client
        self.indicators_timestamp_filename = indicators_timestamp_filename
        self.import_all_indicators = import_all_indicators
        self.delete_outdated = delete_outdated
        self.settings = settings
        self.crowdstrike_org = self.misp.get_organisation(crowdstrike_org_uuid, True)
        self.already_imported = None
        # self.reports_ids = {}
        self.import_settings = import_settings
        self.galaxy_miss_file = import_settings.get("miss_track_file", "no_galaxy_mapping.log")
        self.log: logging.Logger = logger

    def _log_galaxy_miss(self, family: str):
        if self.MISSING_GALAXIES is None:
            if os.path.exists(self.galaxy_miss_file):
                with open(self.galaxy_miss_file, "r", encoding="utf-8") as miss_file:
                    missing = miss_file.read()
                missing = missing.split("\n")
                if "" in missing:
                    missing.remove("")
            else:
                missing = []
            self.MISSING_GALAXIES = missing
        if family not in self.MISSING_GALAXIES:
            self.MISSING_GALAXIES.append(family)
      
    # def get_cs_reports_from_misp(self):
    #     """Retrieve any report events in MISP based upon tag."""
    #     self.log.info("Checking for previous events.")
    #     events = self.misp.search_index(tags=["self.settings["CrowdStrike"]["reports_unique_tag"]"])
    #     for event in events:
    #         if event.get('info'):
    #             self.reports_ids[event.get('info').split(' ', 1)[0]] = event
    #         else:
    #             self.log.warning("Event %s missing info field.", event)

    def process_indicators(self, indicators_mins_before, events_already_imported):
        """Pull and process indicators.

        :param indicators_days_before: in case on an initial run, this is the age of the indicators pulled in days
        :param events_already_imported: the events already imported in misp, to avoid duplicates
        """
        display_banner(banner=INDICATORS_BANNER,
                       logger=self.log,
                       fallback="BEGIN INDICATORS IMPORT",
                       hide_cool_banners=self.import_settings["no_banners"]
                       )
        #self.log.info(INDICATORS_BANNER)
        start_get_events = int((
            datetime.datetime.today() + datetime.timedelta(minutes=-int(min(indicators_mins_before, 20220)))
            ).timestamp())
        if not self.import_settings.get("force", False):
            if os.path.isfile(self.indicators_timestamp_filename):
                with open(self.indicators_timestamp_filename, 'r', encoding="utf-8") as ts_file:
                    line = ts_file.readline()
                    start_get_events = int(line)

        # Let's see if we can't speed this up a bit
        self.already_imported = events_already_imported
        # self.get_cs_reports_from_misp() # Added to occur before
        self.log.info("Started getting indicators from Crowdstrike Intel API and pushing them in MISP.")
        time_send_request = datetime.datetime.now()

        indicators_count = 0
        for indicators_page in self.intel_api_client.get_indicators(start_get_events, self.delete_outdated):
            self.push_indicators(indicators_page)
            indicators_count += len(indicators_page)

        self.log.info("Got %i indicators from the Crowdstrike Intel API.", indicators_count)

        if indicators_count == 0:
            self._note_timestamp(time_send_request.timestamp())
        #else:
            #self.get_cs_reports_from_misp()
            #self.push_indicators(indicators, events_already_imported)

        self.log.info("Finished getting indicators from Crowdstrike Intel API and pushing them in MISP.")

    def push_indicators(self, indicators, events_already_imported = None):
        """Push valid indicators into MISP."""
        def threaded_indicator_push(indicator):
            # if not self.import_all_indicators and len(indicators.get('reports', [])) == 0:
            #     return
            indicator_name = indicator.get('indicator')

            # if self.delete_outdated and indicator_name is not None and indicator.get('deleted', False):
            #     events = self.misp.search_index(eventinfo=indicator_name, pythonify=True)
            #     for event in events:
            #         self.misp.delete_event(event)
            #         try:
            #             events_already_imported.pop(indicator_name)
            #         except Exception as err:
            #             self.log.debug("indicator %s was marked as deleted in intel API but is not stored in MISP."
            #                           " skipping.\n%s",
            #                           indicator_name,
            #                           str(err)
            #                           )
            #         self.log.warning('deleted indicator %s', indicator_name)
            #     return
            # elif indicator_name is not None and events_already_imported.get(indicator_name) is not None:
            #     return
            # else:
                #related_to_a_misp_report = False
            if indicator_name:
                #for report in indicator.get('reports', []):
                # #    event = self.reports_ids.get(report)
                # #    if event:
                # #        related_to_a_misp_report = True
                #         #indicator_object = self.__create_object_for_indicator(indicator)
                # indicator_object = gen_indicator(indicator, self.settings["CrowdStrike"].get("indicators_tags", [])].split(","))
                # if indicator_object:
                #     try:
                #         if isinstance(indicator_object, MISPObject):
                #             self.misp.add_object(event, indicator_object, True)
                #         elif isinstance(indicator_object, MISPAttribute):
                #             self.misp.add_attribute(event, indicator_object, True)
                #     except Exception as err:
                #         self.log.warning("Could not add object or attribute %s for event %s.\n%s",
                #                         indicator_object,
                #                         event,
                #                         str(err)
                #                         )
                # else:
                #     self.log.warning("Indicator %s missing indicator field.", indicator.get('id'))

                #if related_to_a_misp_report or self.import_all_indicators:
                if self.import_all_indicators:
                    self.__add_indicator_event(indicator)
                    if indicator_name is not None:
                        events_already_imported[indicator_name] = True

        if events_already_imported is None:
            events_already_imported = self.already_imported
        with concurrent.futures.ThreadPoolExecutor(self.misp.thread_count, thread_name_prefix="thread") as executor:
            executor.map(threaded_indicator_push, indicators)

        last_updated = next(i.get('last_updated') for i in reversed(indicators) if i.get('last_updated') is not None)
        self._note_timestamp(str(last_updated))

        self.log.info("Pushed %i indicators to MISP.", len(indicators))

    def __add_indicator_event(self, indicator):
        """Add an indicator event for the indicator specified."""
        event = MISPEvent()
        event.analysis = 2
        event.orgc = self.crowdstrike_org
        tag_list = []
        def __update_tag_list(tagging_list:list, tag_value: str):
            _tag = MISPTag()
            _tag.from_dict(name=tag_value)
            tagging_list.append(_tag)
            return tagging_list

        indicator_value = indicator.get("indicator")
        # indicator_type = indicator.get("type").replace("hash_", "")
        if indicator_value:
            #self.log.debug("Reviewing indicator %s", indicator_value)
            #event.info = f"{indicator_value} ({indicator_type.upper()})"
            event.info = indicator_value
            #indicator_object = self.__create_object_for_indicator(indicator)
            indicator_object = gen_indicator(indicator, [])

            if indicator_object:
                if isinstance(indicator_object, MISPObject):
                    event.add_object(indicator_object)
                elif isinstance(indicator_object, MISPAttribute):
                    seen = {}
                    do_timestamp = False
                    if indicator.get("published_date"):
                        do_timestamp = True
                        seen["first_seen"] = indicator.get("published_date")
                    if indicator.get("last_updated"):
                        do_timestamp = True
                        seen["last_seen"] = indicator.get("last_updated")
                    event.add_attribute(indicator_object.type, indicator_object.value, **seen)
                    if do_timestamp:
                        ts = MISPObject("timestamp")
                        if indicator.get("published_date"):
                            ts.add_attribute("first-seen", 
                                datetime.datetime.utcfromtimestamp(indicator.get("published_date")).isoformat()
                            )
                        if indicator.get("last_updated"):
                            ts.add_attribute("last-seen", 
                                datetime.datetime.utcfromtimestamp(indicator.get("last_updated")).isoformat()
                            )
                        ts.add_attribute("precision", "full")
                        event.add_object(ts)
                    #event.add_attribute(indicator_object)
                else:
                    self.log.warning("Couldn't add indicator object to the event corresponding to MISP event %s.",
                                    indicator_value
                                    )
        else:
            self.log.warning("Indicator %s missing indicator field.", indicator.get('id'))

        malicious_confidence = indicator.get('malicious_confidence')
        if malicious_confidence is None:
            self.log.warning("Indicator %s missing malicious_confidence field.", indicator.get('id'))
        else:
            try:
                event.threat_level_id = MaliciousConfidence[malicious_confidence.upper()].value
            except AttributeError:
                self.log.warning("Could not map malicious_confidence level with value %s", malicious_confidence)

        for actor in indicator.get('actors', []):
            for adv in [a for a in dir(Adversary) if "__" not in a]:
                if adv in actor and " " not in actor:
                    actor = actor.replace(adv, f" {adv}")
            ta = event.add_attribute('threat-actor', actor)
            branch = actor.split(" ")[1]
            event.add_attribute_tag(f"CrowdStrike:adversary:branch: {branch}", ta.uuid)
            # Can't cross-tag with this as we're using it for delete
            #event.add_tag(f"CrowdStrike:adversary:branch: {branch}")

        for target in indicator.get('targets', []):
            industry_object = MISPObject('victim')
            industry_object.add_attribute('sectors', target)
            event.add_object(industry_object)

        for threat_type in indicator.get("threat_types"):
            threat = MISPObject("internal-reference")
            threat.add_attribute("identifier", "Threat type", disable_correlation=True)
            tht = threat.add_attribute("comment", threat_type)
            tht.add_tag(f"CrowdStrike:indicator:threat: {threat_type.upper()}")
            event.add_object(threat)

        #for tag in self.settings["CrowdStrike"]["indicators_tags"].split(","):
        #    tag_list = __update_tag_list(tag_list, tag)
        if indicator.get('type', None):
            tag_list = __update_tag_list(tag_list, f"CrowdStrike:indicator:type: {indicator.get('type').upper()}")

        family_found = False
        for malware_family in indicator.get('malware_families', []):
            galaxy = self.import_settings["galaxy_map"].get(malware_family)
            if galaxy is not None:
                tag_list = __update_tag_list(tag_list, galaxy)
                family_found = True

        if not family_found:
            self._log_galaxy_miss(malware_family)
            if confirm_boolean_param(self.settings["TAGGING"].get("taxonomic_WORKFLOW", False)):
                tag_list = __update_tag_list(tag_list, 'workflow:todo="add-missing-misp-galaxy-cluster-values"')
            else:
                tag_list = __update_tag_list(tag_list, self.import_settings["unknown_mapping"])

        labels = [lab.get("name") for lab in indicator.get("labels")]
        for label in labels:
            label = label.lower()
            parts = label.split("/")
            label_val = parts[1]
            label_type = parts[0].lower().replace("killchain", "kill-chain").replace("threattype", "threat")
            label_type = label_type.replace("maliciousconfidence", "malicious-confidence").replace("mitreattck", "mitre-attck")
            if label_type == "actor":
                for adv in [a for a in dir(Adversary) if "__" not in a]:
                    if adv in label_val:
                        label_val = label_val.replace(adv, f" {adv}")
                        actor_proper_name = " ".join([n.title() for n in label_val.split(" ")])
                        actor_att = {
                            "type": "threat-actor",
                            "value": actor_proper_name,
                        }
                        if indicator.get("published_date"):
                            actor_att["first-seen"] = datetime.datetime.utcfromtimestamp(indicator.get("published_date")).isoformat()
                            
                        if indicator.get("last_updated"):
                            actor_att["last-seen"] = datetime.datetime.utcfromtimestamp(indicator.get("last_updated")).isoformat()
                        
                        ta = event.add_attribute(**actor_att)
                        event.add_attribute_tag(f"CrowdStrike:adversary:branch: {adv}", ta.uuid)
                        #event.add_tag(f"CrowdStrike.adversary: {label_val}")
                        #tag_list = __update_tag_list(f"CrowdStrike:indicator:adversary: {label_val}")

            if label_type == "threat":
                scnt = 0
                for s in label_val:
                    scnt += 1
                    if s.isupper() and scnt > 1:
                        label_val = label_val.replace(s, f" {s}")
                threat = MISPObject("internal-reference")
                threat.add_attribute("identifier", "Threat type", disable_correlation=True)
                tht = threat.add_attribute("comment", label_val)
                tht.add_tag(f"CrowdStrike:indicator:threat: {label_val.upper()}")
                event.add_object(threat)

            if label_type == "kill-chain":
                for kc in list(k for k in dir(KillChain) if "__" not in k):
                    if kc == label_val.upper():
                        self.log.debug("Tagging taxonomic kill chain match: kill-chain:%s", KillChain[kc].value)
                        if confirm_boolean_param(self.settings["TAGGING"].get("taxonomic_KILL-CHAIN", False)):
                            event.add_tag(f"kill-chain:{KillChain[kc].value}")

            if label_type in ["malicious-confidence", "kill-chain", "threat", "malware", "mitre-attck", "actor"]:
                label_val = label_val.upper()
            if label_type == "actor":
                label_type = "adversary"
                for act in [a for a in dir(Adversary) if "__" not in a]:
                    if act in label_val:
                        label_val = label_val.replace(act, f" {act}")
                        # Makes deep searches difficult after there's a lot of data
                        #tag_list = __update_tag_list(tag_list, f"CrowdStrike:adversary: {label_val}")
            # Skip these for now
            if label_type not in ["kill-chain"] and confirm_boolean_param(self.settings["TAGGING"].get("taxonomic_KILL-CHAIN", False)):
                tag_list = __update_tag_list(tag_list, f"CrowdStrike:indicator:{label_type}: {label_val}")
            # else:
            #     tag_list = __update_tag_list(tag_list, f"CrowdStrike:indicator:label: {label}")
            #     event.add_attribute("threat-actor", label.upper().replace("ACTOR/", ""))

        for _tag in tag_list:
            #self.log.debug("Indicator event tagged as %s", _tag)
            event.add_tag(_tag)
        # TYPE Taxonomic tag, all events
        if confirm_boolean_param(self.settings["TAGGING"].get("taxonomic_TYPE", False)):
            event.add_tag('type:CYBINT')
        # INFORMATION-SECURITY-DATA-SOURCE Taxonomic tag, all events
        if confirm_boolean_param(self.settings["TAGGING"].get("taxonomic_INFORMATION-SECURITY-DATA-SOURCE", False)):
            event.add_tag('information-security-data-source:integrability-interface="api"')
            event.add_tag('information-security-data-source:originality="original-source"')
            event.add_tag('information-security-data-source:type-of-source="security-product-vendor-website"')
        if confirm_boolean_param(self.settings["TAGGING"].get("taxonomic_IEP", False)):
            event.add_tag('iep:commercial-use="MUST NOT"')
            event.add_tag('iep:provider-attribution="MUST"')
            event.add_tag('iep:unmodified-resale="MUST NOT"')
        if confirm_boolean_param(self.settings["TAGGING"].get("taxonomic_IEP2", False)):
            if confirm_boolean_param(self.settings["TAGGING"].get("taxonomic_IEP2_VERSION", False)):
                event.add_tag('iep2-policy:iep_version="2.0"')
            event.add_tag('iep2-policy:attribution="must"')
            event.add_tag('iep2-policy:unmodified_resale="must-not"')
        if confirm_boolean_param(self.settings["TAGGING"].get("taxonomic_TLP", False)):
            event.add_tag("tlp:amber")

        try:
            self.misp.add_event(event)
            self.log.debug("Successfully added unattributed indicator event for indicator %s", event.info)
        except Exception as err:
            self.log.warning("Could not add event %s.\n%s", event.info, str(err))


    def _note_timestamp(self, timestamp):
        with open(self.indicators_timestamp_filename, 'w', encoding="utf-8") as ts_file:
            ts_file.write(str(int(timestamp)))
        if self.MISSING_GALAXIES:
            for _galaxy in self.MISSING_GALAXIES:
                self.log.warning("No galaxy mapping found for %s malware family.", _galaxy)
        
            with open(self.galaxy_miss_file, "w", encoding="utf-8") as miss_file:
                miss_file.write("\n".join(self.MISSING_GALAXIES))
