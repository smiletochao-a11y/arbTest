import axios from 'axios'

const api = axios.create({
  // 本地开发走 vite proxy 的 /api；ARM 上部署在 /cb/ 下，需走 /cb_api（ARM 的 /api/ 已被 ArbDashboard 占用）
  baseURL: location.pathname.startsWith('/cb') ? '/cb_api' : '/api',
  // 采集接口要跑 AKShare 一览表 + 新浪 300+ 只正股行情，实测约 30 秒；
  // 原来 30s 正好卡在临界点会误报超时，这里放宽到 180s（后端仍在采，只是前端等不及）
  timeout: 180000
})

export interface BondVO {
  bond_code: string
  bond_name: string
  stock_code: string
  stock_name: string
  bond_price: number
  bond_change_pct: number
  stock_price: number
  stock_change_pct: number
  convert_price: number
  convert_value: number
  premium_rate: number
  double_low: number
  pre_tax_yield: number
  pre_tax_put_yield: number
  remaining_years: number
  remaining_size: number
  maturity_date: string
  credit_rating: string
  pure_bond_value: number
  put_value: number
  elasticity: number
  pb_ratio: number
  redeem_trigger_price: number
  put_trigger_price: number
  turnover_rate: number
  turnover_amount: number
  data_date: string
  source: string
}

export interface StrategyResultVO {
  strategy_name: string
  strategy_key: string
  bonds: BondVO[]
  filter_desc: string
  data_date: string
}

export interface DataStatusVO {
  data_date: string
  source: string
  total_count: number
  updated_at: string
}

export const getBonds = (params?: { sort_by?: string; sort_order?: string }) =>
  api.get<BondVO[]>('/bonds', { params })

export const getStrategy = (key: string, n?: number) =>
  api.get<StrategyResultVO>(`/strategy/${key}`, { params: { n } })

export const getStrategies = () =>
  api.get<string[]>('/strategies')

export const getStatus = () =>
  api.get<DataStatusVO>('/status')

export const collectData = (source?: string) =>
  api.post('/collect', null, { params: { source } })
