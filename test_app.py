import pytest
from app import app

@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client

def test_login_page(client):
    response = client.get('/login')
    assert response.status_code == 200
    assert b'login' in response.data.lower()

def test_login_success(client):
    response = client.post('/login', data={
        'username': 'admin',
        'password': 'pass123'
    }, follow_redirects=True)
    assert b'pair' in response.data.lower()  # Should redirect to index page

def test_login_failure(client):
    response = client.post('/login', data={
        'username': 'wrong',
        'password': 'wrong'
    })
    assert b'invalid credentials' in response.data.lower() 