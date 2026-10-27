"""Parsers module"""
from .gpb_parser import GPBParser
from .tender_pro_parser import TenderProParser
from .zakupki_parser import ZakupkiParser
from .roseltorg_parser import RoseltorgParser
from .tektorg_parser import TektorgParser
from .bidzaar_parser import BidzaarParser

__all__ = ['GPBParser', 'TenderProParser', 'ZakupkiParser', 'TektorgParser', 'RoseltorgParser', 'BidzaarParser']
