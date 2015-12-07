from flask import current_app
from datetime import datetime, timedelta
from urlparse import urljoin
from urllib import urlencode
import requests
import grequests
from error import TTAMOAuthError
from ..database import db

TOKEN_URI = 'https://api.23andme.com/token/' 
API_BASE = 'https://api.23andme.com/1/' 

def assert_good_resp(resp):
    '''
    assert that an HTTP response from 23andMe is ok
    '''
    if resp.status_code != 200:
        raise TTAMOAuthError(resp.text)


def api_call(call_func):
    '''
    Decorator of method of TTAMClient that calls 23andme API.
    Updates token in the case of outdated token.
    ''' 
    def checked(self, *args, **kwargs):
        if self.is_expired():
            self.update(current_app.config['TTAM_CONFIG'])
        return call_func(self, *args, **kwargs)

    return checked


# TODO test demo data
class TTAMClient(db.Model): 
    '''
    23andme API client
    '''
    user_id = db.Column(db.String(200), db.ForeignKey('User.email'), primary_key=True)
    access_token = db.Column(db.String(150), nullable=True)
    refresh_token = db.Column(db.String(150), nullable=True)
    expire_at = db.Column(db.DateTime, nullable=True)
    # might be a demo account
    api_base = db.Column(db.String(200), nullable=True)
    profiles = db.Column(db.Text, nullable=True)
    
    def __init__(self, code, user_id, ttam_config): 
        '''
        Initialize a 23andme client given an authorization code
        by exchanging access_token with the code.
        '''
        post_data = {
            'client_id': ttam_config['client_id'],
            'client_secret': ttam_config['client_secret'],
            'grant_type': 'authorization_code',
            'redirect_uri': ttam_config['redirect_uri'],
            'scope': ttam_config['scope'],
            'code': code
        }
        resp = requests.post(TOKEN_URI, data=post_data)
        assert_good_resp(resp)
        self._set_tokens(resp.json())
        # see if need to use demo data
        self.set_api_base()
        patients = self.get_patients()
        self.profiles = ' '.join(p['id'] for p in patients)
        self.user_id = user_id 

    def set_api_base(self):
        '''
        Check if the user has genetic data,
        if not, use 23andme's demo data
        '''
        self.api_base = API_BASE
        if len(self.get_patients()) == 0:
            self.api_base = urljoin(API_BASE, 'demo')

    def _set_tokens(self, credentials):
        '''
        set tokens (access and refresh) and calculate expiration time
        '''
        self.access_token = credentials['access_token']
        self.refresh_token = credentials['refresh_token']
        # just to be safe, set expire time 100 seconds earlier than acutal expire time
        self.expire_at = datetime.now() + timedelta(seconds=int(credentials['expires_in']-100))

    def is_expired(self):
        '''
        check if the client's tokens have expired
        '''
        return datetime.now() > self.expire_at 

    def update(self, ttam_config):
        '''
        update tokens
        '''
        post_data = {
            'client_id': ttam_config['client_id'],
            'client_secret': ttam_config['client_secret'],
            'grant_type': 'refresh_token',
            'redirect_uri': ttam_config['redirect_uri'],
            'scope': ttam_config['scope'],
            'refresh_token': self.refresh_token
        }
        update_resp = requests.post(TOKEN_URI, data=post_data)
        assert_good_resp(update_resp)
        self._set_tokens(update_resp.json())
        db.session.add(self)
        db.session.commit()

    @api_call
    def get_snps(self, query, pids=None):
        '''
        given a list of rsids, and patients to which queried SNPs belongs to,
        return a list of SNPs in this format
        ```
        {
            [patient id]: {
                [rsid]: [genotype],
                ...
            }
        }
        ```
        '''
        if pids is None:
            pids = self.get_profiles()
        api_endpoint = urljoin(self.api_base, 'genotypes/')
        snps_str = ' '.join(query)
        args = {'locations': snps_str, 'format': 'embedded'}
        urls = (urljoin(api_endpoint, p)+"?"+urlencode(args)
                for p in pids)
        auth_header = self._get_header() 
        reqs = (grequests.get(u, headers=auth_header) for u in urls)
        resps = grequests.map(reqs) 
        if any(resp.status_code != 200 for resp in resps):
            raise TTAMOAuthError(map(lambda r: r.text, resps))
        patient_data = (resp.json() for resp in resps) 
        return {pdata['id']: pdata['genotypes'] for pdata in patient_data}

    def _get_header(self):
        '''
        helper functions for getting HTTP Header to make 23andme API call
        '''
        return {'x-access-token': {self.access_token}}

    @api_call
    def get_patients(self):
        '''
        get all profiles owned by the user who authorized this client
        '''
        auth_header = self._get_header()
        resp = requests.get(urljoin(self.api_base, 'names/'), headers=auth_header)
        assert_good_resp(resp)
        return resp.json()['profiles']

    def get_profiles(self):
        return self.profiles.split()
