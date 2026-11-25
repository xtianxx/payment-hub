import http from 'k6/http';
import { check } from 'k6';
import exec from 'k6/execution';
import { Counter, Rate } from 'k6/metrics';

const STAGE = __ENV.STAGE || 'smoke';
const RATE = Number(__ENV.RATE || '2');
const DURATION = __ENV.DURATION || '5s';
const BASE_URL = __ENV.BASE_URL || '';
const API_KEY = __ENV.PERF_API_KEY || '';
const ORDER_PREFIX = __ENV.ORDER_PREFIX || '';
const PRE_VUS = Number(__ENV.PRE_VUS || '4');
const MAX_VUS = Number(__ENV.MAX_VUS || '8');
const SUMMARY_PATH = __ENV.SUMMARY_PATH || 'payment-create-summary.json';

const iterationStarted = new Counter('payment_iteration_started');
const businessOK = new Rate('payment_business_ok');
const responseReceived = new Rate('payment_response_received');
const businessFail = new Counter('payment_business_fail');
const issues = new Counter('payment_issues');
const timeout = new Counter('payment_timeout');
const transport = new Counter('payment_transport_error');
const status5xx = new Counter('payment_status_5xx');
const status4xx = new Counter('payment_status_4xx');
const statusOther = new Counter('payment_status_other');

export const options = {
  scenarios: {
    [STAGE]: {
      executor: 'constant-arrival-rate',
      rate: RATE,
      timeUnit: '1s',
      duration: DURATION,
      preAllocatedVUs: PRE_VUS,
      maxVUs: MAX_VUS,
      gracefulStop: '10s',
    },
  },
  // Correctness guard only, not an SLO. Transport coverage is separate.
  thresholds: {
    payment_business_ok: ['rate==1'],
    payment_response_received: ['rate>0'],
  },
  noConnectionReuse: false,
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
};

export function setup() {
  if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(BASE_URL)) {
    throw new Error('BASE_URL must be a fixed loopback http://127.0.0.1:<port> URL');
  }
  if (!API_KEY) {
    throw new Error('PERF_API_KEY is required');
  }
  if (!/^[A-Za-z0-9]{1,40}$/.test(ORDER_PREFIX)) {
    throw new Error('ORDER_PREFIX must be 1..40 alphanumeric characters');
  }
  if (!/^[A-Za-z0-9_-]{1,32}$/.test(STAGE)) {
    throw new Error('STAGE must be 1..32 [A-Za-z0-9_-]');
  }
  if (!(RATE > 0 && RATE <= 400)) {
    throw new Error('RATE must be in (0, 400]');
  }
  if (!(PRE_VUS > 0 && MAX_VUS >= PRE_VUS && MAX_VUS <= 128)) {
    throw new Error('VUs must satisfy 0 < PRE_VUS <= MAX_VUS <= 128');
  }
  console.log(`stage=${STAGE} rate=${RATE}/s duration=${DURATION} maxVUs=${MAX_VUS} prefix=${ORDER_PREFIX}`);
}

export default function () {
  // Lightweight start counter at iteration entry. k6's built-in `iterations`
  // metric counts completed iterations, so this counter is the explicit start
  // count used by the runner.
  iterationStarted.add(1, { stage: STAGE });
  const seq = exec.scenario.iterationInTest;
  if (typeof seq !== 'number' || seq < 0) {
    throw new Error('scenario.iterationInTest unavailable');
  }
  const orderNo = `${ORDER_PREFIX}${String(seq).padStart(8, '0')}`;
  const body = JSON.stringify({
    business_order_no: orderNo,
    amount: 100,
    currency: 'CNY',
  });
  const res = http.post(`${BASE_URL}/api/v1/payments`, body, {
    headers: {
      'Authorization': `Bearer ${API_KEY}`,
      'Content-Type': 'application/json',
      'Accept': 'application/json',
    },
    timeout: '2s',
    redirects: 0,
    throw: false,
    tags: { stage: STAGE },
  });

  responseReceived.add(res.status !== 0 ? 1 : 0, { stage: STAGE });
  if (res.status >= 500) status5xx.add(1, { stage: STAGE });
  else if (res.status >= 400) status4xx.add(1, { stage: STAGE });
  else if (res.status !== 0 && res.status !== 201) statusOther.add(1, { stage: STAGE });

  let parsed = null;
  try {
    parsed = res.json();
  } catch (e) {
    parsed = null;
  }
  const payment = parsed && typeof parsed === 'object' ? parsed.payment : null;
  const hasIssues = !!(parsed && typeof parsed === 'object' &&
    Object.prototype.hasOwnProperty.call(parsed, 'issues'));
  const statusOK = res.status === 201;
  const resultOK = !!(payment && payment.result === 'SUCCEEDED');
  const moneyOK = !!(payment && payment.amount === 100 && payment.currency === 'CNY');
  const business = statusOK && resultOK && !hasIssues && moneyOK;

  if (res.status !== 0) {
    businessOK.add(business, { stage: STAGE });
    if (!business) businessFail.add(1, { stage: STAGE });
  }
  if (hasIssues) issues.add(1, { stage: STAGE });
  if (res.error_code === 1050 || (res.error && String(res.error).toLowerCase().includes('timeout'))) {
    timeout.add(1, { stage: STAGE });
  } else if (res.error) {
    transport.add(1, { stage: STAGE, error_code: String(res.error_code || 'unknown') });
  }
  if (!business) {
    // Failure-only evidence; order IDs are not metric labels.
    const rid = parsed && parsed.request_id ? parsed.request_id : '';
    console.warn(
      `ANOMALY order=${orderNo} status=${res.status} ` +
      `result=${payment && payment.result ? payment.result : 'missing'} ` +
      `issues=${hasIssues} error_code=${res.error_code || 0} request_id=${rid}`,
    );
  }

  check(res, {
    'status is 201': (r) => r.status === 201,
    'payment.result is SUCCEEDED': () => resultOK,
    'no issues': () => !hasIssues,
    'amount is 100 CNY': () => moneyOK,
  }, { stage: STAGE });
}

export function handleSummary(data) {
  return {
    [SUMMARY_PATH]: JSON.stringify(data, null, 2) + '\n',
  };
}
